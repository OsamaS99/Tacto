"""Play cases with an agent against the arena over HTTP - the hosted arena (practice, sandbox,
final) or `arena-sim`, the local contract simulator. Both speak the same API.

An agent is any object with `decide(case, threads) -> {"supplier_id": ..., "message": {...}}`:
  threads: {supplier_id: [message, ...]} - every e-mail so far, both directions, in order;
           supplier attachments carry their text under "text" (see `docs.read_document`).
  message: subject, body, up to 3 term sheets, refs - or an "action" that closes the case:
           {"type": "accept", "term_sheet_id": ...} or {"type": "walk_away"}.
"""

import functools
import inspect
import logging
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx

from .client import ENDED as GONE  # ENDED here: the statuses that end a thread
from .client import INVALID, LATE, OPEN_SESSIONS, QUOTA, RATE, ROUND_LIMIT, SERVER, ArenaError, classify
from .cost import must_haves_ok, total_cost
from .docs import read_document, standard_terms

log = logging.getLogger("arena")

ENDED = {"walked_away", "no_bid"}
# A message later than this after the supplier's last one counts as a timeout.
MOVE_LIMIT_S = 60
# In the Live Final, close this long before the wave's cap - or the slowest round of the case so far
# (your decision plus a supplier reply of up to about a minute) plus a margin, if that is longer.
CLOSE_BEFORE_CAP_S, ROUND_MARGIN_S = 90, 15
RUNNING = re.compile(r"one session per case in the final: (\S+)")
# Errors after which a case plays on from the arena's own thread: nothing happened (422, rate limit),
# it came late or the thread ended (409), or it is unknown whether it arrived (5xx, a lost connection).
PLAY_ON = {INVALID, RATE, LATE, GONE, ROUND_LIMIT, SERVER}
MAX_INVALID, MAX_ERRORS = 2, 4  # rejected moves in a row before the closing move; one more and we stop
# All 10 sessions of the key open: start the case again a little later, a few times.
OPEN_RETRIES, OPEN_BACKOFF_S = 5, 3.0
# A case that exhausts a resume attempt can reattach while its wave still has time.
CASE_RESUME_RETRIES, CASE_RESUME_BACKOFF_S = 2, 3.0
# Practice: the case list shows every practice case file; only those marked hosted can be played
# (a session on another is a 403). Without the marker every listed case counts as hosted.
HOSTED_FIELD = "hosted"


class RemoteTable:
    """The arena over HTTP: the practice arena, the final, or a hosted sandbox."""

    def __init__(self, client, case=None):
        self.client, self.session_id, self.case, self.done = client, None, case, None

    def open(self, case_id):
        try:
            r = self.client.open_session(case_id)
        except ArenaError as e:
            if e.status != 409 or not self.case:
                raise
            running = RUNNING.search(str(e.detail))
            session_id = running.group(1) if running else self.running(case_id)
            if session_id is None:
                raise
            return self.resume(session_id)
        self.session_id = r["session_id"]
        log.info("session %s", self.session_id)
        return r["case"], r["threads"]

    def running(self, case_id):
        """Our final session on a case (an open one first) - when the 409 does not name it."""
        mine = [s for s in self.client.sessions("final") if s.get("case_id") == case_id]
        return max(reversed(mine), key=lambda s: s.get("status") == "open")["session_id"] if mine else None

    def resume(self, session_id):
        """Pick up a final session this team already started (e.g. after a crash): every thread
        so far, from the arena."""
        self.session_id = session_id
        log.info("resuming session %s", session_id)
        state = self.client.session(session_id)
        if state["status"] == "ended":
            self.done = self.result()
        return self.case, {s: self.thread(s) for s in state["threads"]}

    def send(self, supplier_id, message):
        return self.client.send_message(self.session_id, supplier_id, message)

    def close(self, supplier_id, message):
        reply = self.client.send_message(self.session_id, supplier_id, message)
        return reply.get("result", reply)  # the supplier's last e-mail carries the result

    def attachment(self, attachment_id):
        return self.client.attachment(self.session_id, attachment_id)

    def thread(self, supplier_id):
        """The thread as the arena has it: every e-mail, ours included - never one it did not take.
        If its offer or status moved without an e-mail (a timeout answer, the thread ended), an
        arena note at the end says so."""
        t = self.client.thread(self.session_id, supplier_id)
        events, last = list(t["messages"]), latest(t["messages"])
        sheets = last.get("term_sheets") or [{}]
        ended = t.get("closed") and last.get("status") not in ENDED
        shown = (sheets[0].get("term_sheet_id"), last.get("status"))
        moved = not t.get("closed") and t.get("timeouts") and (t.get("offer_id"), t.get("status")) != shown
        if ended or moved:
            answer = (t.get("timeouts") or [{}])[-1]
            sheet = {"term_sheet_id": t.get("offer_id"), "terms": answer.get("terms") or sheets[0].get("terms", {})}
            events.append(
                {
                    "from": "arena",
                    "status": "walked_away" if ended else t["status"],
                    "term_sheets": [] if ended else [sheet],
                    "body": f"({t.get('closed') or 'timeout'})",
                    "missing_fields": answer.get("missing_fields", []),
                    "attachments": [],
                }
            )
        return events

    def ended(self):
        """The result if the case has ended (e.g. at a wave's cap), else None."""
        state = self.client.session(self.session_id)
        if state.get("status") == "ended":
            return state.get("result") or {"deal": False, "end_reason": state.get("end_reason")}
        return None

    def result(self):
        state = self.client.session(self.session_id)
        return state.get("result") or {"deal": False, "end_reason": state.get("end_reason"), **state}

    def timing(self):
        """Authoritative remaining buyer time; callers capture their request time first."""
        return self.client.session(self.session_id)


def latest(events):
    """The supplier's latest message in a thread."""
    return next(m for m in reversed(events) if m.get("from") != "buyer")


def standing(events):
    """(term_sheet_id, terms) of the supplier's standing term sheet, or (None, {})."""
    sheets = latest(events).get("term_sheets") or []
    return (sheets[0]["term_sheet_id"], sheets[0]["terms"]) if sheets else (None, {})


def live_threads(threads):
    """Suppliers whose thread has not ended."""
    return [sid for sid, ev in threads.items() if latest(ev).get("status") not in ENDED]


def moves_left(case, events):
    """Messages you may still send in this thread (the round limit counts each of them)."""
    return case.get("round_limit", 10) - sum(m.get("from") == "buyer" for m in events)


def fetch_documents(table, message):
    """Download every attachment of a supplier message and add its text."""
    for att in message.get("attachments") or []:
        if att.get("attachment_id") and "text" not in att:
            att["text"] = read_document(att["name"], table.attachment(att["attachment_id"]))
    return message


def closing_move(case, threads):
    """Accept the cheapest standing term sheet that keeps the must-haves and beats the alternative
    (C with each supplier's standard terms), else walk away - what to do when time runs out."""
    alt = case["buyer"].get("alternative") or {}
    c_alt = total_cost(case, alt["terms"]) if alt.get("terms", {}).get("unit_price") else float("inf")
    best = None
    for sid in live_threads(threads):
        sheet_id, terms = standing(threads[sid])
        std = standard_terms(threads[sid])
        if sheet_id and must_haves_ok(case, terms, std) and (c := total_cost(case, terms, std)) < c_alt:
            best = min(best or (c, sid, sheet_id), (c, sid, sheet_id))
    if best is None:
        return {
            "supplier_id": (live_threads(threads) or list(threads))[0],
            "message": {"action": {"type": "walk_away"}},
        }
    return {"supplier_id": best[1], "message": {"action": {"type": "accept", "term_sheet_id": best[2]}}}


def refresh(table, supplier_id, known):
    """The thread as the arena has it, keeping the documents we already read."""
    texts = {a.get("attachment_id"): a["text"] for m in known for a in m.get("attachments") or [] if "text" in a}
    events = table.thread(supplier_id)
    for m in events:
        for att in m.get("attachments") or []:
            if (aid := att.get("attachment_id")) and aid in texts:
                att.setdefault("text", texts[aid])
        fetch_documents(table, m)
    return events


def _read_retry(fn, attempts=3):
    """Reconciliation reads survive transient failures without inventing a new move."""
    for attempt in range(attempts + 1):
        try:
            return fn()
        except (ArenaError, httpx.TransportError) as e:
            if classify(e) not in (SERVER, RATE) or attempt == attempts:
                raise
            time.sleep(min(2.0, 0.25 * 2**attempt))


def _close_decision(agent, case, threads, reason):
    close = getattr(agent, "close_decision", None)
    return close(case, threads, reason) if close else closing_move(case, threads)


def play(table, case_id, agent, deadline=None):
    """A single coordinator owns histories, deadlines and closure; workers only send HTTP.
    Context-aware agents receive authoritative clocks; old decide(case, threads) agents still work.
    """
    from .buyer.records import TurnContext

    if hasattr(agent, "deadline"):
        agent.deadline = deadline
    if hasattr(table, "store") and hasattr(agent, "store"):
        agent.store = table.store
    prepare_started = time.monotonic()
    if getattr(table, "case", None) and hasattr(agent, "prepare"):
        # Reattachment already has buyer clocks running: restore the durable brief after open,
        # rather than spend a fresh model-backed preparation before reading those clocks.
        if getattr(table, "resume_expected", False) and "allow_model" in inspect.signature(agent.prepare).parameters:
            agent.prepare(table.case, allow_model=False)
        else:
            agent.prepare(table.case)
    preparation_ms = round(1000 * (time.monotonic() - prepare_started))
    case, threads = table.open(case_id)
    opened_at = time.monotonic()
    if hasattr(agent, "set_turn_context"):
        agent.session_id = getattr(table, "session_id", None)
    if getattr(table, "done", None):
        return table.done
    due = {sid: opened_at + MOVE_LIMIT_S for sid in threads}
    input_ready = {}

    def sync_timing():
        if not hasattr(table, "timing"):
            return
        requested = time.monotonic()
        state = _read_retry(table.timing)
        for sid, thread in (state.get("threads") or {}).items():
            remaining = thread.get("seconds_left")
            if sid in due and isinstance(remaining, int | float):
                # Conservatively include request latency, rather than grant time on read.
                due[sid] = requested + max(0.0, remaining)

    sync_timing()  # before downloads, model work, or a resumed agent starts thinking
    for sid, events in threads.items():
        for message in events:
            fetch_documents(table, message)
        input_ready[sid] = time.monotonic()
    slowest, invalid, errors = 0.0, 0, 0
    limited, pending, recovering = set(), {}, set()
    parallel = max(1, min(len(threads), getattr(table, "supplier_parallel", 1)))
    closing_reason = None

    def context(closing=False):
        now = time.monotonic()
        cap = deadline() if callable(deadline) else deadline
        wave = now + cap - time.time() if cap else None
        available = [sid for sid in live_threads(threads) if sid not in pending]
        end = min((due[sid] for sid in available), default=now + MOVE_LIMIT_S) - 5.0
        if wave is not None:
            end = min(end, wave - ROUND_MARGIN_S)
        ctx = TurnContext(
            session_id=getattr(table, "session_id", None),
            observed_at=now,
            due_at=dict(due),
            revisions={sid: len(events) for sid, events in threads.items()},
            wave_deadline=wave,
            decision_deadline=end,
            closing=closing,
            in_flight=tuple(pending),
        )
        if hook := getattr(agent, "set_turn_context", None):
            hook(ctx)
        return ctx

    def note(sid, started, **extra):
        if not hasattr(table, "note"):
            return
        finished = time.monotonic()
        received = due[sid] - MOVE_LIMIT_S
        table.note.update(
            {
                **(getattr(agent, "event", None) or {}),
                "decision_ms": round(1000 * (finished - started)),
                "decision_started_at": started,
                "decision_finished_at": finished,
                "receipt_at": received,
                "input_ready_at": input_ready.get(sid, started),
                "input_ms": round(1000 * max(0, input_ready.get(sid, started) - received)),
                "receipt_to_decision_ms": round(1000 * max(0, started - received)),
                "preparation_ms": preparation_ms,
                **extra,
            }
        )

    def error(sid, exc):
        nonlocal invalid, errors
        kind = classify(exc)
        if kind not in PLAY_ON:
            raise exc
        errors, invalid = errors + 1, invalid + (kind == INVALID)
        log.warning("%s: %s %s", sid, kind, getattr(exc, "detail", exc))
        if result := _read_retry(table.ended):
            return result
        if errors > MAX_ERRORS or invalid > MAX_INVALID:
            raise exc
        threads[sid] = _read_retry(lambda: refresh(table, sid, threads[sid]))
        input_ready[sid] = time.monotonic()
        if kind == ROUND_LIMIT:
            limited.add(sid)
        sync_timing()
        return None

    def send(sid, message):
        reply = table.send(sid, message)
        return reply, time.monotonic()  # before attachments or coordinator scheduling

    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="supplier") as pool:
        while live_threads(threads) or pending:
            for sid, (future, message, started) in list(pending.items()):
                if not future.done():
                    continue
                del pending[sid]
                try:
                    reply, received = future.result()
                except (ArenaError, httpx.TransportError) as exc:
                    recovering.discard(sid)
                    if result := error(sid, exc):
                        return result
                    continue
                invalid = errors = 0
                if "result" in reply:
                    return reply["result"]
                due[sid] = received + MOVE_LIMIT_S
                if sid in recovering:
                    recovering.remove(sid)
                    threads[sid] = _read_retry(lambda sid=sid: refresh(table, sid, threads[sid]))
                    sync_timing()
                else:
                    threads[sid] += [{**message, "from": "buyer"}, fetch_documents(table, reply)]
                input_ready[sid] = time.monotonic()
                slowest = max(slowest, time.monotonic() - started)

            cap = deadline() if callable(deadline) else deadline
            if cap and time.time() > cap - max(CLOSE_BEFORE_CAP_S, slowest + ROUND_MARGIN_S):
                closing_reason = closing_reason or "wave cap near"
            if invalid >= MAX_INVALID or errors >= MAX_ERRORS:
                closing_reason = closing_reason or "repeated rejected moves"

            # Ambiguous POSTs settle under their original ID before another mutation of that thread.
            if hasattr(table, "pending_moves"):
                for move in table.pending_moves():
                    if move.sid not in pending:

                        def recover(sid=move.sid):
                            return table.recover_pending(sid), time.monotonic()

                        started = time.monotonic()
                        note(move.sid, started, source="recovery")
                        if hasattr(table, "queue_note"):
                            table.queue_note(move.sid)
                        recovering.add(move.sid)
                        pending[move.sid] = (pool.submit(recover), move.payload, started)

            if closing_reason:
                if hasattr(table, "begin_closing"):
                    table.begin_closing()
                if pending:
                    wait([f for f, _, _ in pending.values()], timeout=0.1, return_when=FIRST_COMPLETED)
                    continue
                if result := _read_retry(table.ended):
                    return result
                # Drain, then refresh every rival before choosing one current standing offer.
                for sid in list(threads):
                    threads[sid] = _read_retry(lambda sid=sid: refresh(table, sid, threads[sid]))
                    input_ready[sid] = time.monotonic()
                sync_timing()
                context(True)
                started = time.monotonic()
                decision = _close_decision(agent, case, threads, closing_reason)
                sid, message = decision["supplier_id"], decision["message"]
                note(sid, started, reason=closing_reason)
                try:
                    return table.close(sid, message)
                except (ArenaError, httpx.TransportError) as exc:
                    if result := error(sid, exc):
                        return result
                    continue

            available = [sid for sid in live_threads(threads) if sid not in pending]
            if not available or len(pending) >= parallel:
                if pending:
                    wait([f for f, _, _ in pending.values()], timeout=0.1, return_when=FIRST_COMPLETED)
                    continue
                break
            ctx = context()
            started = time.monotonic()
            if ctx.decision_deadline <= started:
                decision = _close_decision(agent, case, threads, "buyer deadline near")
            else:
                decision = agent.decide(case, threads)
            sid, message = decision["supplier_id"], decision["message"]
            if message.get("action"):
                closing_reason = "agent requested closure"
                continue
            if time.monotonic() >= ctx.decision_deadline:
                closing_reason = "decision deadline elapsed"
                continue
            if any(future.done() for future, _, _ in pending.values()):
                # A rival changed while the model was selecting: incorporate its canonical
                # reply/error and recompute the economic reservation before any new dispatch.
                continue
            if sid in limited:
                closing_reason = "round limit"
                continue
            if sid not in available:
                if pending:  # an old agent unaware of in-flight requests waits for its selected thread
                    wait([f for f, _, _ in pending.values()], timeout=0.1, return_when=FIRST_COMPLETED)
                    continue
                raise ValueError(f"agent selected unavailable supplier {sid}")
            note(sid, started)
            if hasattr(table, "queue_note"):
                table.queue_note(sid)
            pending[sid] = (pool.submit(send, sid, message), message, started)
    return _read_retry(table.result)


def hosted(cases):
    """The cases you can play: those marked hosted, or all of them if the list has no marker."""
    if any(HOSTED_FIELD in c for c in cases):
        return [c for c in cases if c.get(HOSTED_FIELD)]
    return cases


def quota_used_up(error):
    """The practice quota's 429 ("practice limit: …"): retrying does not help before it resets."""
    return classify(error) == QUOTA


def play_retrying(new_table, case_id, new_agent, deadline=None, resume=False):
    """`play`, started again after a short wait while the key has its 10 sessions open."""
    for attempt in range(OPEN_RETRIES + 1):
        try:
            table = new_table()
            saved = getattr(table, "has_saved_session", None)
            table.resume_expected = resume and (attempt > 0 or bool(saved and saved(case_id)))
            return play(table, case_id, new_agent(), deadline)
        except (ArenaError, httpx.TransportError) as e:
            if (
                classify(e) not in ({OPEN_SESSIONS, SERVER, RATE} if resume else {OPEN_SESSIONS})
                or attempt == OPEN_RETRIES
            ):
                raise
            log.info("%s: %s - trying again", case_id, getattr(e, "detail", e))
            delay = OPEN_BACKOFF_S * (attempt + 1)
            cap = deadline() if callable(deadline) else deadline
            if resume and cap and time.time() + delay >= cap - ROUND_MARGIN_S:
                raise
            time.sleep(delay)


def play_all(new_table, case_ids, new_agent, parallel=8):
    """Play many cases at once (e.g. every practice case). `new_table()` and `new_agent()` give
    each case its own table and agent. Once the practice quota is used up, no further case starts;
    a case that finds every session slot taken starts again a little later.
    Returns {case_id: result or the exception that stopped it}."""
    used_up = threading.Event()

    def one(case_id):
        case = case_id if isinstance(case_id, dict) else None
        case_id = case["case_id"] if case else case_id
        if used_up.is_set():
            return case_id, ArenaError(429, "not started: the practice quota is used up")
        try:

            def table():
                value = new_table()
                if case is not None:
                    value.case = case
                return value

            return case_id, play_retrying(table, case_id, new_agent)
        except ArenaError as e:
            if quota_used_up(e):
                used_up.set()
                log.warning("practice quota used up: %s", e.detail)
            elif e.status == 403:
                log.warning("%s: %s", case_id, e.detail)  # e.g. not hosted for practice
            else:
                log.exception("%s failed", case_id)
            return case_id, e
        except Exception as e:  # one broken case must not stop the others
            log.exception("%s failed", case_id)
            return case_id, e

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return dict(pool.map(one, case_ids))


def play_waves(client, new_agent, waves=5, parallel=10, poll=5.0, new_table=RemoteTable):
    """The Live Final: wait for each wave, play its cases in parallel within the wave's cap, wait
    for the next. Keep it running from before the first wave until the last has closed; it returns
    after `waves` waves have closed and every case has finished: {case_id: result or exception}.
    `new_table(client, case)`: the table per case (it knows the case, so the agent prepares first)."""
    started, closed, caps = {}, set(), {}  # caps: wave -> its cap as time.time()
    resume_counts, retry_at, failed_future = {}, {}, {}

    def one(case, deadline):
        try:
            return play_retrying(
                functools.partial(new_table, client, case), case["case_id"], new_agent, deadline, resume=True
            )
        except Exception as e:  # one broken case must not stop the others
            log.exception("%s failed", case["case_id"])
            return e

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        while len(closed) < waves or not all(f.done() for f in started.values()):
            try:
                cases = client.cases("final")
            except (ArenaError, httpx.TransportError) as e:
                if classify(e) in (SERVER, RATE):
                    log.warning("final case list temporarily unavailable: %s", e)
                    time.sleep(poll)
                    continue
                if not isinstance(e, ArenaError) or e.status != 403:  # before the first wave
                    raise
                cases = []
            for case in cases:
                if not case.get("open"):
                    closed.add(case["wave"])
                    continue
                if case.get("seconds_left") is not None:  # each wave has its own cap; follow an extension
                    caps[case["wave"]] = time.time() + case["seconds_left"]
                case_id = case["case_id"]
                future = started.get(case_id)
                if future is not None:
                    if not future.done() or not isinstance(failure := future.result(), Exception):
                        continue
                    if classify(failure) not in {SERVER, RATE, OPEN_SESSIONS}:
                        continue
                    count = resume_counts.get(case_id, 0)
                    if count >= CASE_RESUME_RETRIES:
                        continue
                    if failed_future.get(case_id) is not future:
                        failed_future[case_id] = future
                        retry_at[case_id] = time.monotonic() + CASE_RESUME_BACKOFF_S * (count + 1)
                    if time.monotonic() < retry_at[case_id]:
                        continue
                    if caps.get(case["wave"], 0) - time.time() <= ROUND_MARGIN_S:
                        continue
                    resume_counts[case_id] = count + 1
                    log.warning("%s reattaching after %s (attempt %s)", case_id, classify(failure), count + 1)
                if case.get("seconds_left"):
                    if future is None and case["wave"] not in {c["wave"] for c in cases if c["case_id"] in started}:
                        log.warning("wave %s started: %s", case["wave"], case.get("theme"))
                    started[case_id] = pool.submit(one, case, functools.partial(caps.get, case["wave"]))
            time.sleep(poll)
    return {case_id: f.result() for case_id, f in started.items()}
