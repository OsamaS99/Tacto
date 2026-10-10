"""Sending that survives a crash. Every move is written down with its X-Move-Id before the POST, so a
restarted run re-sends it with the same id and the arena answers it once instead of playing it
twice. A move is done once the arena answered, dropped once it refused (late, ended, invalid), and
left pending when the answer was lost (5xx, a dropped connection) until same-ID replay settles it."""

import hashlib
import json
import logging
import os
import threading
import time
import uuid

import httpx

from ..client import RATE, SERVER, ArenaError, classify
from ..play import RemoteTable
from .records import OutboxMove
from .store import Store, _safe

log = logging.getLogger("arena")

__all__ = ["DurableTable", "classify"]


def _sha(message):
    return hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _ours(events):
    return sum(m.get("from") == "buyer" for m in events)


class DurableTable(RemoteTable):
    """A RemoteTable whose moves are kept in a Store (default: runs/, or $BUYER_RUNS)."""

    def __init__(self, client, case=None, store=None, supplier_parallel=None):
        super().__init__(client, case)
        self.store = store or Store()
        self.note = {}  # what play() and the agent say about the next move, for its event line
        self.sent = {}  # supplier -> our messages the arena holds, as far as we know
        self.supplier_parallel = (
            supplier_parallel
            if supplier_parallel is not None
            else (3 if os.environ.get("BUYER_CONCURRENT", "0") == "1" else 1)
        )
        self._lock, self._notes, self.closing = threading.RLock(), {}, False
        self._recorded_result = False

    def has_saved_session(self, case_id):
        """Final cases have one session: a saved snapshot means preparation is already done."""
        return any(self.store.root.glob(f"*/{_safe(case_id)}/*.json"))

    def open(self, case_id):
        case, threads = super().open(case_id)
        self.case = case
        self.store.begin(self.session_id, case_id, (case or {}).get("set") or case_id)
        # Opening reads canonical histories only. The case coordinator replays unresolved
        # moves in supplier workers, so a slow restarted RFQ cannot block every other thread.
        self.sent = {sid: _ours(ev) for sid, ev in threads.items()}
        return case, threads

    def reconcile(self, threads):
        """Re-send every pending move exactly as it was, with its id: the arena answers what it
        already did, or does it now. A refusal drops it. Then the threads as the arena has them."""
        for move in self.pending_moves():
            try:
                reply = self.recover_pending(move.sid)
            except ArenaError as e:
                if classify(e) in (SERVER, RATE):
                    raise
                continue
            if "result" in reply:
                self.done = reply["result"]
                break
        return {sid: self.thread(sid) for sid in threads}

    def send(self, supplier_id, message):
        return self._post(supplier_id, message)

    def close(self, supplier_id, message):
        reply = self._post(supplier_id, message)
        return reply.get("result", reply)

    def thread(self, supplier_id):
        """Read canonical history without treating message absence as delivery evidence."""
        # A GET may precede completion of an uncertain POST. Neither absence nor a count of
        # identical historical messages proves which move arrived: only same-ID replay settles it.
        events = super().thread(supplier_id)
        self.sent[supplier_id] = _ours(events)
        return events

    def pending_moves(self, supplier_id=None):
        return [m for m in self.store.load(self.session_id) if supplier_id is None or m.sid == supplier_id]

    def recover_pending(self, supplier_id):
        moves = self.pending_moves(supplier_id)
        if not moves:
            raise ArenaError(409, "no unresolved move remains")
        with self._lock:
            note = self._notes.pop(supplier_id, {})
        return self._send_move(
            moves[0],
            {**note, "source": "recovery", "outbox_age_ms": round(1000 * max(0, time.time() - moves[0].created_at))},
        )

    def begin_closing(self):
        with self._lock:
            self.closing = True

    def queue_note(self, supplier_id):
        """Capture this decision before a worker starts; another supplier cannot overwrite it."""
        with self._lock:
            self._notes[supplier_id] = dict(self.note)
            self.note.clear()

    def ended(self):
        result = super().ended()
        if result is not None:
            self._settle_ended(result)
            self._record_result(result)
        return result

    def result(self):
        result = super().result()
        self._settle_ended(result)
        self._record_result(result)
        return result

    def _record_result(self, result):
        if not self._recorded_result:
            self.store.event(self.session_id, kind="result", result=result)
            self._recorded_result = True

    def _settle_ended(self, result):
        for move in self.pending_moves():
            action = move.payload.get("action") or {}
            ours = (
                action.get("type") == "accept"
                and result.get("deal")
                and result.get("supplier_id") == move.sid
                and result.get("offer_id") == action.get("term_sheet_id")
            )
            self._finish(move, "done" if ours else "failed", None if ours else "session_ended")

    def _post(self, supplier_id, message):
        with self._lock:
            if self.closing and not message.get("action"):
                raise ArenaError(409, "closing barrier: proposals are stopped")
            if self.pending_moves(supplier_id):
                raise ArenaError(503, "resolve the prior move under its original ID before sending another")
            move = OutboxMove(uuid.uuid4().hex, self.session_id, supplier_id, message, _sha(message), time.time())
            self.store.put(move)  # atomically recorded before a worker can send it
            note = self._notes.pop(supplier_id, dict(self.note))
            self.note.clear()
        return self._send_move(move, note)

    def _send_move(self, move, note):
        supplier_id, message = move.sid, move.payload
        note = dict(note)
        entered = time.monotonic()

        def dispatched(at):
            # First HTTP attempt only: retries retain the original decision and delivery age.
            if "dispatched_at" in note:
                return
            note["dispatched_at"] = at
            note["admission_wait_ms"] = round(1000 * max(0, at - entered))
            for field, metric in (
                ("decision_started_at", "decision_to_dispatch_ms"),
                ("receipt_at", "receipt_age_ms"),
                ("input_ready_at", "ready_to_dispatch_ms"),
            ):
                if field in note:
                    note[metric] = round(1000 * max(0, at - note[field]))

        options = {"on_dispatch": dispatched} if getattr(self.client, "supports_dispatch_callback", False) else {}
        try:
            if not options:
                dispatched(entered)  # direct adapters have no separate rate/network admission queue
            reply = self.client.send_message(self.session_id, supplier_id, message, move_id=move.move_id, **options)
        except (ArenaError, httpx.TransportError) as e:
            kind = classify(e)
            self._finish(move, "pending" if kind in (SERVER, RATE) else "failed", kind or str(e))
            self._event(supplier_id, message, error=kind or str(e), http_status=getattr(e, "status", None), note=note)
            raise
        self._finish(move, "done")
        if not message.get("action"):
            self.sent[supplier_id] = self.sent.get(supplier_id, 0) + 1
        self._event(supplier_id, message, reply, note=note)
        if "result" in reply:
            self._recorded_result = True  # already carried by the move's event
        return reply

    def _finish(self, move, state, error=None):
        move.state, move.error = state, error
        self.store.put(move)

    def _event(self, supplier_id, message, reply=None, error=None, http_status=None, note=None):
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
            "error_kind": error,
            "http_status": http_status,
            "case_id": (self.case or {}).get("case_id"),
            **(note or {}),
        }
        self.store.event(self.session_id, **fields)
