"""Deadline, concurrency and ambiguous-delivery regressions without network/provider calls."""

import copy
import json
import threading
from pathlib import Path

import pytest

from arena_kit.buyer.store import Store
from arena_kit.buyer.transport import DurableTable
from arena_kit.client import ArenaError
from arena_kit.play import closing_move, play, play_waves


def case():
    value = json.loads((Path(__file__).parents[1] / "arena_kit/sim_cases/S01.json").read_text())
    value["suppliers"] = ["sup:A", "sup:B"]
    return value


def offer(sid, price, rev=0):
    return {
        "from": sid,
        "status": "open",
        "body": "Please find our quotation.",
        "attachments": [],
        "term_sheets": [{"term_sheet_id": f"offer:{sid}-{rev}", "terms": {"unit_price": price, "payment_days": 60}}],
    }


class Table:
    """A deterministic, delayed two-supplier API, with an observable closing barrier."""

    supplier_parallel = 2
    session_id = "s1"

    def __init__(self):
        self.case = case()
        self.events = {"sup:A": [offer("sup:A", 16)], "sup:B": [offer("sup:B", 17)]}
        self.release = threading.Event()
        self.started = threading.Event()
        self.result_value = None
        self.inflight = set()
        self.log = []
        self.lock = threading.Lock()

    def open(self, case_id):
        return self.case, copy.deepcopy(self.events)

    def timing(self):
        return {"threads": {sid: {"seconds_left": 60} for sid in self.events}}

    def send(self, sid, message):
        with self.lock:
            assert sid not in self.inflight
            self.inflight.add(sid)
            self.log.append(("send", sid))
        if sid == "sup:A":
            self.started.set()
            assert self.release.wait(2), "the coordinator failed to reach the close barrier"
        else:
            assert self.started.wait(1)
        reply = offer(sid, 14 if sid == "sup:A" else 15, 1)
        with self.lock:
            self.events[sid] += [{**message, "from": "buyer"}, reply]
            self.inflight.remove(sid)
            self.log.append(("reply", sid))
        return copy.deepcopy(reply)

    def begin_closing(self):
        self.log.append(("barrier", None))
        self.release.set()

    def thread(self, sid):
        return copy.deepcopy(self.events[sid])

    def ended(self):
        return self.result_value

    def close(self, sid, message):
        assert not self.inflight, "accept overlapped an outstanding supplier mutation"
        assert message["action"]["term_sheet_id"] == self.events[sid][-1]["term_sheets"][0]["term_sheet_id"]
        self.log.append(("close", sid))
        self.result_value = {"deal": True, "supplier_id": sid}
        return self.result_value

    def result(self):
        return self.result_value


class Agent:
    def __init__(self):
        self.contexts = []

    def prepare(self, value):
        pass

    def set_turn_context(self, context):
        self.contexts.append(context)

    def decide(self, value, threads):
        pending = self.contexts[-1].in_flight
        sent = [sid for sid, ev in threads.items() if any(m.get("from") == "buyer" for m in ev)]
        if sent:
            return {"supplier_id": "sup:B", "message": {"action": {"type": "accept", "term_sheet_id": "stale"}}}
        sid = next(s for s in threads if s not in pending)
        return {
            "supplier_id": sid,
            "message": {"body": "Proposal", "term_sheets": [{"terms": {"unit_price": 13, "payment_days": 60}}]},
        }

    def close_decision(self, value, threads, reason):
        return closing_move(value, threads)


def test_rfq_serves_other_supplier_and_drains_before_refreshed_accept():
    table, agent = Table(), Agent()
    result = play(table, table.case["case_id"], agent)
    assert result == {"deal": True, "supplier_id": "sup:A"}
    assert table.log.index(("send", "sup:B")) < table.log.index(("reply", "sup:A"))
    assert table.log[-1] == ("close", "sup:A")
    assert any(c.in_flight == ("sup:A",) for c in agent.contexts)
    assert agent.contexts[-1].closing


def test_runner_clock_precedes_attachment_work(monkeypatch):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    now = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runtime, "fetch_documents", lambda table, message: now.__setitem__(0, now[0] + 5) or message)
    table, agent = Table(), Agent()
    table.case["suppliers"] = ["sup:A"]
    table.events = {"sup:A": [offer("sup:A", 16)]}
    table.timing = lambda: {"threads": {"sup:A": {"seconds_left": 25}}}
    agent.decide = lambda value, threads: closing_move(value, threads)
    result = play(table, table.case["case_id"], agent)
    first = agent.contexts[0]
    assert result["deal"] and first.observed_at == 105
    assert first.due_at["sup:A"] == 125 and first.decision_deadline == 120


class Ambiguous:
    def __init__(self):
        self.ids = []
        self.events = [offer("sup:A", 16)]
        self.fail = True

    def send_message(self, session, sid, message, move_id=None):
        self.ids.append(move_id)
        if self.fail:
            self.fail = False
            raise ArenaError(503, "still processing")
        self.events += [{**message, "from": "buyer"}, offer(sid, 15, 1)]
        return self.events[-1]

    def thread(self, session, sid):
        return {"messages": self.events, "closed": None, "timeouts": [], "offer_id": "offer:sup:A-0", "status": "open"}


def test_get_absence_keeps_move_pending_and_recovery_reuses_id(tmp_path):
    client = Ambiguous()
    table = DurableTable(client, store=Store(tmp_path))
    table.session_id = "s1"
    table.store.begin("s1", "case:T", "unit")
    message = {"body": "Proposal"}
    with pytest.raises(ArenaError):
        table.send("sup:A", message)
    table.thread("sup:A")
    assert len(table.pending_moves()) == 1
    with pytest.raises(ArenaError, match="prior move"):
        table.send("sup:A", {"body": "A different proposal"})
    assert len(client.ids) == 1
    table.recover_pending("sup:A")
    assert len(set(client.ids)) == 1 and table.pending_moves() == []
    assert sum(m.get("from") == "buyer" for m in client.events) == 1


def test_barrier_rejects_new_proposals_but_allows_pending_recovery(tmp_path):
    client = Ambiguous()
    table = DurableTable(client, store=Store(tmp_path))
    table.session_id = "s1"
    table.store.begin("s1", "case:T", "unit")
    with pytest.raises(ArenaError):
        table.send("sup:A", {"body": "Proposal"})
    table.begin_closing()
    with pytest.raises(ArenaError, match="closing barrier"):
        table.send("sup:B", {"body": "Another proposal"})
    table.recover_pending("sup:A")
    assert len(set(client.ids)) == 1


def test_final_poll_recovers_transient_failure(monkeypatch):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)

    class Client:
        calls = 0

        def cases(self, set_name):
            self.calls += 1
            if self.calls == 1:
                raise ArenaError(503, "temporary outage")
            return [{"case_id": "case:F1", "wave": 1, "open": False}]

    client = Client()
    assert play_waves(client, Agent, waves=1, poll=0) == {}
    assert client.calls == 2


class InProcess:
    """Direct contract calls, with the same idempotency behavior as the HTTP adapter."""

    def __init__(self, lost=False):
        from arena_kit.sim import Sim

        value = case()
        value["suppliers"] = ["sup:A"]
        self.sim = Sim({value["case_id"]: value}, no_clock=True)
        self.cache, self.lost, self.ids = {}, lost, []

    def open_session(self, cid):
        return copy.deepcopy(self.sim.open("team", cid))

    def session(self, sid):
        return copy.deepcopy(self.sim.state("team", sid))

    def thread(self, sid, supplier):
        return copy.deepcopy(self.sim.thread("team", sid, supplier))

    def attachment(self, sid, aid):
        return self.sim.attachment("team", sid, aid)[0]

    def send_message(self, sid, supplier, message, move_id=None):
        from arena_kit.sim import Fail

        self.ids.append(move_id)
        if move_id in self.cache:
            return copy.deepcopy(self.cache[move_id])
        try:
            reply = copy.deepcopy(self.sim.message("team", sid, supplier, message))
        except Fail as exc:
            raise ArenaError(exc.status, exc.detail) from exc
        self.cache[move_id] = reply
        if self.lost:
            self.lost = False
            raise ArenaError(503, "response lost after execution")
        return reply


def test_runner_recovers_executed_proposal_once_and_then_accepts(tmp_path):
    client, agent = InProcess(lost=True), Agent()
    value = next(iter(client.sim.cases.values()))
    table = DurableTable(client, value, Store(tmp_path))
    out = play(table, value["case_id"], agent)
    assert out["deal"] and table.pending_moves() == []
    assert client.ids[0] == client.ids[1]
    canonical = client.thread(table.session_id, "sup:A")["messages"]
    assert sum(m.get("from") == "buyer" for m in canonical) == 1
    events = [json.loads(line) for line in next(tmp_path.rglob("*.events.jsonl")).read_text().splitlines()]
    assert events[0]["error_kind"] == "SERVER" and events[0]["http_status"] == 503
    assert events[-1]["result"]["deal"]


def test_two_invalid_proposals_use_safe_close_and_log_its_decision(tmp_path):
    client, agent = InProcess(), Agent()
    value = next(iter(client.sim.cases.values()))
    calls = []

    def invalid(value, threads):
        calls.append(1)
        return {"supplier_id": "sup:A", "message": {"term_sheets": [{"terms": {"unit_price": 13}}]}}

    agent.decide = invalid
    table = DurableTable(client, value, Store(tmp_path))
    assert play(table, value["case_id"], agent)["deal"] and len(calls) == 2
    events = [json.loads(line) for line in next(tmp_path.rglob("*.events.jsonl")).read_text().splitlines()]
    assert [e["error_kind"] for e in events] == ["INVALID", "INVALID", None]
    assert all("decision_ms" in e for e in events)


def test_failed_final_case_is_retried_for_session_resume(monkeypatch):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    calls = []

    def interrupted(table, cid, agent, deadline):
        calls.append(cid)
        if len(calls) == 1:
            raise ArenaError(503, "uncertain previous mutation")
        return {"deal": True}

    monkeypatch.setattr(runtime, "play", interrupted)
    monkeypatch.setattr(runtime, "OPEN_BACKOFF_S", 0)
    assert runtime.play_retrying(Table, "case:F1", Agent, resume=True) == {"deal": True}
    assert calls == ["case:F1", "case:F1"]


def test_restart_open_retains_pending_without_waiting_for_supplier(tmp_path):
    from arena_kit.buyer.records import OutboxMove

    class Client(Ambiguous):
        sends = 0

        def open_session(self, cid):
            self.sends_at_open = self.sends
            return {"session_id": "s1", "case": case(), "threads": {"sup:A": self.events}}

        def send_message(self, *args, **kwargs):
            self.sends += 1
            raise AssertionError("open must leave uncertain deliveries for coordinator workers")

    client, store = Client(), Store(tmp_path)
    store.begin("s1", "case:T", case().get("set") or "case:T")
    store.put(OutboxMove("original", "s1", "sup:A", {"body": "pending"}, "x", 1.0))
    table = DurableTable(client, case(), store)
    _, threads = table.open("case:T")
    assert client.sends == 0 and len(threads["sup:A"]) == 1
    assert [move.move_id for move in table.pending_moves()] == ["original"]


def test_dispatch_timing_includes_pacing_and_keeps_first_attempt(monkeypatch, tmp_path):
    import importlib

    transport = importlib.import_module("arena_kit.buyer.transport")
    now = [110.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])

    class Client:
        supports_dispatch_callback = True

        def send_message(self, session, sid, message, move_id=None, *, on_dispatch=None):
            now[0] = 113.0  # admission to the HTTP request after pacing
            on_dispatch(now[0])
            now[0] = 119.0  # a later retry does not reset delivery age
            on_dispatch(now[0])
            return offer(sid, 15)

    table = DurableTable(Client(), store=Store(tmp_path))
    table.session_id = "s1"
    table.store.begin("s1", "case:T", "unit")
    table.note.update(decision_started_at=106.0, receipt_at=100.0, input_ready_at=105.0)
    table.send("sup:A", {"body": "Proposal"})
    event = json.loads(next(tmp_path.rglob("*.events.jsonl")).read_text())
    assert event["admission_wait_ms"] == 3000
    assert event["decision_to_dispatch_ms"] == 7000
    assert event["receipt_age_ms"] == 13000 and event["ready_to_dispatch_ms"] == 8000
    assert event["dispatched_at"] == 113.0


def test_input_and_preparation_latency_logged_on_proposal_and_closure(monkeypatch, tmp_path):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    now = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runtime, "fetch_documents", lambda table, message: now.__setitem__(0, now[0] + 2) or message)
    client, agent = InProcess(), Agent()
    value = next(iter(client.sim.cases.values()))
    agent.prepare = lambda value: now.__setitem__(0, now[0] + 3)
    table = DurableTable(client, value, Store(tmp_path))
    assert play(table, value["case_id"], agent)["deal"]
    events = [json.loads(line) for line in next(tmp_path.rglob("*.events.jsonl")).read_text().splitlines()]
    assert len(events) == 2
    assert all(event["preparation_ms"] == 3000 for event in events)
    assert events[0]["input_ms"] > 0 and events[0]["receipt_age_ms"] >= events[0]["input_ms"]
    assert all("decision_to_dispatch_ms" in event and "receipt_age_ms" in event for event in events)


@pytest.mark.parametrize(("status", "succeeds", "expected"), [(503, True, 2), (503, False, 3), (401, False, 1)])
def test_final_case_resubmission_is_bounded_and_transient_only(monkeypatch, status, succeeds, expected):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    attempts, complete, success = [], threading.Event(), threading.Event()

    def attempt(new_table, cid, new_agent, deadline, resume):
        assert resume
        attempts.append(cid)
        complete.set()
        if succeeds and len(attempts) == 2:
            success.set()
            return {"deal": True}
        raise ArenaError(status, "temporary outage" if status == 503 else "unknown key")

    monkeypatch.setattr(runtime, "play_retrying", attempt)
    monkeypatch.setattr(runtime, "CASE_RESUME_BACKOFF_S", 0)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: threading.Event().wait(0.001))

    class Client:
        polls = 0

        def cases(self, name):
            self.polls += 1
            assert self.polls < 100, "failed cases were neither bounded nor resumed"
            if self.polls > 1:
                assert complete.wait(1)
            closed = success.is_set() or (len(attempts) >= expected and self.polls > 5)
            return [{"case_id": "case:F1", "wave": 1, "open": not closed, "seconds_left": 600}]

    out = play_waves(Client(), Agent, waves=1, poll=0)
    assert len(attempts) == expected
    if succeeds:
        assert out == {"case:F1": {"deal": True}}
    else:
        assert isinstance(out["case:F1"], ArenaError) and out["case:F1"].status == status


def test_restarted_rfq_recovers_slow_supplier_without_starving_rival():
    from types import SimpleNamespace

    class Restarted(Table):
        def __init__(self):
            super().__init__()
            self.unresolved = [SimpleNamespace(sid="sup:A", payload={"body": "Original proposal"})]

        def pending_moves(self):
            return self.unresolved

        def recover_pending(self, sid):
            reply = self.send(sid, self.unresolved[0].payload)
            self.unresolved = []
            return reply

    table, agent = Restarted(), Agent()
    assert play(table, table.case["case_id"], agent)["deal"]
    assert table.log.index(("send", "sup:B")) < table.log.index(("reply", "sup:A"))
    assert table.log[-1] == ("close", "sup:A") and not table.unresolved


def test_elapsed_decision_deadline_closes_without_dispatching_stale_proposal(monkeypatch):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    now = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    table, agent = Table(), Agent()
    table.events = {"sup:A": [offer("sup:A", 16)]}

    def delayed(value, threads):
        now[0] = 160.0
        return {"supplier_id": "sup:A", "message": {"body": "Expired proposal"}}

    agent.decide = delayed
    assert play(table, table.case["case_id"], agent)["deal"]
    assert not any(kind == "send" for kind, _ in table.log)
    assert table.log[-1] == ("close", "sup:A")


def test_rival_reply_completed_during_decision_invalidates_unposted_proposal(monkeypatch):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    completed = threading.Event()
    original_pool = runtime.ThreadPoolExecutor

    class Pool(original_pool):
        def submit(self, fn, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            if args and args[0] == "sup:A":
                future.add_done_callback(lambda _: completed.set())
            return future

    monkeypatch.setattr(runtime, "ThreadPoolExecutor", Pool)
    table, agent = Table(), Agent()
    calls = []

    def choose(value, threads):
        calls.append(len(threads["sup:A"]))
        if len(calls) == 1:
            return {"supplier_id": "sup:A", "message": {"body": "First proposal"}}
        if len(calls) == 2:
            table.release.set()
            assert completed.wait(1)
            return {"supplier_id": "sup:B", "message": {"body": "Stale proposal based on old rival quote"}}
        return closing_move(value, threads)

    agent.decide = choose
    assert play(table, table.case["case_id"], agent)["deal"]
    assert calls == [1, 1, 3] and ("send", "sup:B") not in table.log
    assert table.log[-1] == ("close", "sup:A")


@pytest.mark.parametrize("saved", [False, True])
def test_final_saved_session_uses_deterministic_preparation_before_reattachment(saved):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    flags = []

    class Saved(Table):
        def __init__(self):
            super().__init__()
            self.events = {"sup:A": [offer("sup:A", 16)]}

        def has_saved_session(self, cid):
            return saved

    class Prepared(Agent):
        def prepare(self, value, *, allow_model=True):
            flags.append(allow_model)

        def decide(self, value, threads):
            return closing_move(value, threads)

    assert runtime.play_retrying(Saved, case()["case_id"], Prepared, resume=True)["deal"]
    assert flags == [not saved]


def test_final_retry_skips_new_model_preparation_after_first_attempt(monkeypatch):
    import importlib

    runtime = importlib.import_module("arena_kit.play")
    monkeypatch.setattr(runtime, "OPEN_BACKOFF_S", 0)
    opens, flags = [], []

    class Retry(Table):
        def __init__(self):
            super().__init__()
            self.events = {"sup:A": [offer("sup:A", 16)]}

        def open(self, cid):
            opens.append(cid)
            if len(opens) == 1:
                raise ArenaError(503, "lost open-session response")
            return super().open(cid)

    class Prepared(Agent):
        def prepare(self, value, *, allow_model=True):
            flags.append(allow_model)

        def decide(self, value, threads):
            return closing_move(value, threads)

    assert runtime.play_retrying(Retry, case()["case_id"], Prepared, resume=True)["deal"]
    assert flags == [True, False]


def test_three_independently_delayed_suppliers_close_after_all_pending_replies():
    import time

    class Three(Table):
        supplier_parallel = 3

        def __init__(self):
            super().__init__()
            self.case["suppliers"].append("sup:C")
            self.events["sup:C"] = [offer("sup:C", 18)]
            self.all_sent = threading.Event()

        def send(self, sid, message):
            with self.lock:
                self.inflight.add(sid)
                self.log.append(("send", sid))
                if len(self.inflight) == 3:
                    self.all_sent.set()
            assert self.all_sent.wait(2), "supplier replies must not serialize behind a global lock"
            time.sleep({"sup:A": 0.04, "sup:B": 0.02, "sup:C": 0.01}[sid])
            reply = offer(sid, {"sup:A": 14, "sup:B": 15, "sup:C": 16}[sid], 1)
            with self.lock:
                self.events[sid] += [{**message, "from": "buyer"}, reply]
                self.inflight.remove(sid)
                self.log.append(("reply", sid))
            return reply

    table, agent = Three(), Agent()
    result = play(table, table.case["case_id"], agent)
    assert result["supplier_id"] == "sup:A"
    first_reply = next(i for i, x in enumerate(table.log) if x[0] == "reply")
    assert all(("send", sid) in table.log[:first_reply] for sid in table.events)
    assert table.log[-1] == ("close", "sup:A")
