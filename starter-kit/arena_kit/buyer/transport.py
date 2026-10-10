"""Sending that survives a crash. Every move is written down with its X-Move-Id before the POST, so a
restarted run re-sends it with the same id and the arena answers it once instead of playing it
twice. A move is done once the arena answered, dropped once it refused (late, ended, invalid), and
left pending when the answer was lost (5xx, a dropped connection) - until the thread tells."""

import hashlib
import json
import logging
import time
import uuid

import httpx

from ..client import SERVER, ArenaError, classify
from ..play import RemoteTable
from .records import OutboxMove
from .store import Store

log = logging.getLogger("arena")

__all__ = ["DurableTable", "classify"]


def _sha(message):
    return hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _ours(events):
    return sum(m.get("from") == "buyer" for m in events)


class DurableTable(RemoteTable):
    """A RemoteTable whose moves are kept in a Store (default: runs/, or $BUYER_RUNS)."""

    def __init__(self, client, case=None, store=None):
        super().__init__(client, case)
        self.store = store or Store()
        self.note = {}  # what play() and the agent say about the next move, for its event line
        self.sent = {}  # supplier -> our messages the arena holds, as far as we know

    def open(self, case_id):
        case, threads = super().open(case_id)
        self.store.begin(self.session_id, case_id, (case or {}).get("set") or case_id)
        if not self.done and self.store.load(self.session_id):  # a restart with moves in flight
            threads = self.reconcile(threads)
        self.sent = {sid: _ours(ev) for sid, ev in threads.items()}
        return case, threads

    def reconcile(self, threads):
        """Re-send every pending move exactly as it was, with its id: the arena answers what it
        already did, or does it now. A refusal drops it. Then the threads as the arena has them."""
        for move in self.store.load(self.session_id):
            try:
                reply = self.client.send_message(self.session_id, move.sid, move.payload, move_id=move.move_id)
            except ArenaError as e:
                if (kind := classify(e)) == SERVER:
                    raise
                log.info("%s: pending move %s dropped (%s)", move.sid, move.move_id, kind)
                self._finish(move, "failed", kind)
                continue
            self._finish(move, "done")
            if "result" in reply:
                self.done = reply["result"]
        return {sid: self.thread(sid) for sid in threads}

    def send(self, supplier_id, message):
        return self._post(supplier_id, message)

    def close(self, supplier_id, message):
        reply = self._post(supplier_id, message)
        return reply.get("result", reply)

    def thread(self, supplier_id):
        """The arena's thread; it also settles a pending move to this supplier: one more message
        of ours than we knew of means it arrived."""
        events = super().thread(supplier_id)
        if supplier_id in self.sent:  # not while resuming: reconcile settles those
            for move in self.store.load(self.session_id):
                if move.sid == supplier_id:
                    self._finish(move, "done" if _ours(events) > self.sent[supplier_id] else "failed")
            self.sent[supplier_id] = _ours(events)
        return events

    def _post(self, supplier_id, message):
        move = OutboxMove(uuid.uuid4().hex, self.session_id, supplier_id, message, _sha(message), time.time())
        self.store.put(move)  # written down before it can reach the arena
        try:
            reply = self.client.send_message(self.session_id, supplier_id, message, move_id=move.move_id)
        except (ArenaError, httpx.TransportError) as e:
            kind = classify(e)
            self._finish(move, "pending" if kind == SERVER else "failed", kind or str(e))
            self._event(supplier_id, message, error=kind or str(e))
            raise
        self._finish(move, "done")
        if not message.get("action"):
            self.sent[supplier_id] = self.sent.get(supplier_id, 0) + 1
        self._event(supplier_id, message, reply)
        return reply

    def _finish(self, move, state, error=None):
        move.state, move.error = state, error
        self.store.put(move)

    def _event(self, supplier_id, message, reply=None, error=None):
        reply = reply or {}
        fields = {
            "supplier": supplier_id,
            "kind": (message.get("action") or {}).get("type") or "send",
            "packages": [s.get("terms") for s in message.get("term_sheets") or []],
            "refs": message.get("refs") or [],
            "status": reply.get("status"),
            "claim_results": reply.get("claim_results"),
            "result": reply.get("result"),
            "error": error,
            **self.note,  # reason, source, decision_ms, packages with C - from play() and the agent
        }
        self.note = {}
        self.store.event(self.session_id, **fields)
