"""Transport: move ids, the outbox across a restart, and how play() treats rejected, late and lost
moves - against the in-process arena-sim or a fake client."""

import json
import secrets
import sys
import time

import httpx
import pytest

from arena_kit import ArenaClient, ArenaError, RemoteTable, play, play_all
from arena_kit.buyer.records import OutboxMove
from arena_kit.buyer.store import Store
from arena_kit.buyer.transport import DurableTable, classify
from arena_kit.play import standing

SUP = "sup:70270"  # the one supplier of case:S01


def team(url):
    key = ArenaClient(url).register(f"test-{secrets.token_hex(3)}", "sim")["api_key"]
    return ArenaClient(url, key, rate=1000)


def ours(events):
    return sum(m.get("from") == "buyer" for m in events)


def send(price, **terms):
    return {"supplier_id": SUP, "message": {"term_sheets": [{"terms": {"unit_price": price, **terms}}]}}


def accept(threads):
    return {"supplier_id": SUP, "message": {"action": {"type": "accept", "term_sheet_id": standing(threads[SUP])[0]}}}


def store_pending(root):
    snapshot = json.loads(next(root.glob("*/*/*.json")).read_text())
    return [m for m in snapshot["outbox"].values() if m["state"] == "pending"]


class Scripted:
    """Plays the given moves in order and keeps every view of the threads it was shown."""

    def __init__(self, *moves):
        self.moves, self.seen = list(moves), []

    def decide(self, case, threads):
        self.seen.append({s: list(ev) for s, ev in threads.items()})
        move = self.moves.pop(0)
        return move(threads) if callable(move) else move


class Wrapped:
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)


class LostReply(Wrapped):
    """The arena takes the first message, but its reply is lost (a 503 after the client's retries)."""

    lost = False

    def send_message(self, *args, **kw):
        reply = self.client.send_message(*args, **kw)
        if not self.lost:
            self.lost = True
            raise ArenaError(503, "upstream timed out")
        return reply


class Final(Wrapped):
    """The final: a second start of a case is a 409 - here one that does not name the session."""

    def open_session(self, case_id):
        raise ArenaError(409, "final case already played")


def test_durable_table_defaults_to_three_supplier_workers(tmp_path):
    table = DurableTable(object(), store=Store(tmp_path))
    assert table.supplier_parallel == 3


def test_explicit_supplier_parallel_remains_authoritative(tmp_path):
    table = DurableTable(object(), store=Store(tmp_path), supplier_parallel=1)
    assert table.supplier_parallel == 1


def test_a_move_id_is_kept_across_the_client_retries():
    seen = []

    def handle(request):
        seen.append(request.headers["X-Move-Id"])
        if len(seen) == 1:
            return httpx.Response(429, json={"detail": "more than 10 calls per second"}, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"status": "open"})

    c = ArenaClient("http://arena.test", "k", transport=httpx.MockTransport(handle), rate=1000)
    assert c.send_message("s1", SUP, {"body": "Hello"}, move_id="m1")["status"] == "open"
    assert seen == ["m1", "m1"]


def test_a_restart_resends_pending_moves_with_their_ids(sim, tmp_path):
    url, state, _ = sim
    client, case = team(url), state.cases["case:S01"]
    first = DurableTable(client, case, store=Store(tmp_path))
    first.open("case:S01")
    sid, now = first.session_id, time.time()
    arrived, unsent = send(15.0, payment_days=60)["message"], send(14.0, payment_days=60)["message"]
    first.store.put(OutboxMove("m-arrived", sid, SUP, arrived, "x", now))
    client.send_message(sid, SUP, arrived, move_id="m-arrived")  # the arena got it; then the run died
    first.store.put(OutboxMove("m-unsent", sid, SUP, unsent, "y", now + 1))  # died before this POST

    store = Store(tmp_path)  # the restarted run: the 409 names no session, the session list does
    again = DurableTable(Final(client), case, store=store)
    _, threads = again.open("case:S01")
    assert again.session_id == sid and len(store.load(sid)) == 2
    assert ours(threads[SUP]) == 1  # opening does not synchronously replay mutations
    while again.pending_moves(SUP):
        again.recover_pending(SUP)
    threads[SUP] = again.thread(SUP)
    assert store.load(sid) == []
    assert ours(threads[SUP]) == 2 == state.sessions[sid]["threads"][SUP]["round"]  # each played once


def test_an_invalid_move_is_never_recorded_and_play_decides_again(sim, tmp_path):
    agent = Scripted(send(15.0), send(15.0, payment_days=60), accept)  # the first lacks payment_days
    table = DurableTable(team(sim[0]), store=Store(tmp_path))
    assert play(table, "case:S01", agent)["deal"]
    assert ours(agent.seen[1][SUP]) == 0 and ours(agent.seen[2][SUP]) == 1
    events = [json.loads(line) for line in next(tmp_path.glob("*/*/*.events.jsonl")).read_text().splitlines()]
    assert [(e["kind"], e["error"]) for e in events] == [("send", "INVALID"), ("send", None), ("accept", None)]
    assert all("decision_ms" in e for e in events) and store_pending(tmp_path) == []


def test_two_invalid_moves_in_a_row_close_the_case(sim):
    agent = Scripted(send(15.0), send(15.0), send(15.0))
    assert play(RemoteTable(team(sim[0])), "case:S01", agent)["deal"]  # closing_move takes the opening
    assert len(agent.seen) == 2


def test_a_late_message_leaves_no_phantom_in_the_thread(sim):
    url, _, clock = sim

    def late(threads):
        clock.now += 61  # the arena counts a timeout and answers 409
        return send(15.0, payment_days=60)

    agent = Scripted(late, accept)
    assert play(RemoteTable(team(url)), "case:S01", agent)["deal"]
    assert ours(agent.seen[1][SUP]) == 0


def test_a_lost_reply_is_read_back_from_the_thread(sim, tmp_path):
    agent = Scripted(send(15.0, payment_days=60), accept)
    table = DurableTable(LostReply(team(sim[0])), store=Store(tmp_path))
    assert play(table, "case:S01", agent)["deal"]
    thread = agent.seen[1][SUP]
    assert ours(thread) == 1 and thread[-1]["from"] != "buyer"  # our message and its answer, once
    assert store_pending(tmp_path) == []


def test_at_the_round_limit_only_a_close_is_left(sim):
    url, state, _ = sim
    state.cases["case:S01"]["round_limit"] = 1
    agent = Scripted(*[send(15.0, payment_days=60)] * 3)
    assert play(RemoteTable(team(url)), "case:S01", agent)["deal"]  # the third move becomes an accept
    assert len(agent.seen) == 3


def test_the_agent_prepares_before_the_session_opens(sim):
    url, state, _ = sim

    class Prepared(Scripted):
        deadline = None

        def prepare(self, case):
            self.open_at_prepare = len(state.sessions)

    agent, cap = Prepared(accept), time.time() + 3600
    play(RemoteTable(team(url), state.cases["case:S01"]), "case:S01", agent, deadline=cap)
    assert agent.open_at_prepare == 0 and agent.deadline == cap


class Table:
    """A table whose case is over as soon as it opens - unless `open` fails first."""

    def __init__(self, fail):
        self.fail = fail

    def open(self, case_id):
        self.fail(case_id)
        return {"case_id": case_id}, {"sup:A": [{"from": "sales@A", "status": "walked_away", "term_sheets": []}]}

    def result(self):
        return {"ended": True, "deal": False}


def test_a_case_waits_for_a_free_session_slot(monkeypatch):
    monkeypatch.setattr(sys.modules["arena_kit.play"], "OPEN_BACKOFF_S", 0)
    opens = []

    def busy(case_id):
        opens.append(case_id)
        if len(opens) < 3:
            raise ArenaError(429, "at most 10 open sessions per key")

    out = play_all(lambda: Table(busy), ["case:T01"], Scripted)
    assert out == {"case:T01": {"ended": True, "deal": False}} and len(opens) == 3


def test_the_quota_stops_new_starts():
    opens = []

    def used_up(case_id):
        opens.append(case_id)
        raise ArenaError(429, "practice limit: 200 sessions per team in 24 hours")

    out = play_all(lambda: Table(used_up), ["case:T01", "case:T02"], Scripted, parallel=1)
    assert opens == ["case:T01"] and "not started" in out["case:T02"].detail


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ArenaError(409, "late: more than 60 s after the supplier's last move"), "LATE"),
        (ArenaError(409, "the session has ended: wave_cap"), "ENDED"),
        (ArenaError(409, "the thread with sup:1 has ended: timeout"), "ENDED"),
        (ArenaError(409, "round limit reached: accept the standing offer or walk away"), "ROUND_LIMIT"),
        (ArenaError(409, "one session per case in the final: s1"), "ALREADY_PLAYED"),
        (ArenaError(422, "not a standing offer in this session"), "INVALID"),
        (ArenaError(429, "more than 10 calls per second"), "RATE"),
        (ArenaError(429, "at most 10 open sessions per key"), "OPEN_SESSIONS"),
        (ArenaError(429, "practice limit: 200 sessions per team in 24 hours"), "QUOTA"),
        (ArenaError(503, "unavailable"), "SERVER"),
        (httpx.ConnectError("connection reset"), "SERVER"),
        (ArenaError(401, "missing or unknown X-API-Key"), None),
    ],
)
def test_classify(error, kind):
    assert classify(error) == kind


def test_the_store_keeps_pending_moves_memo_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv("BUYER_RUNS", str(tmp_path))
    store = Store()
    store.begin("s1", "case:T01", "practice")
    a, b = (OutboxMove(f"m{i}", "s1", "sup:A", {"body": str(i)}, "x", float(i)) for i in (1, 2))
    store.put(a)
    store.put(b)
    b.state = "done"
    store.put(b)
    assert [m.move_id for m in Store().load("s1")] == ["m1"]  # a fresh store finds the file
    assert store.memo("s1", step=2) == {"step": 2} and Store().memo("s1") == {"step": 2}
    store.event("s1", supplier="sup:A", kind="send")
    line = (tmp_path / "practice" / "case_T01" / "s1.events.jsonl").read_text()
    assert json.loads(line)["kind"] == "send" and not list(tmp_path.rglob("*.tmp"))
