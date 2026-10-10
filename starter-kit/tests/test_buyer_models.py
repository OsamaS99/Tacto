"""Synthetic provider, deadline and interpretation failures; no live model requests."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from arena_kit.buyer import model as model_mod
from arena_kit.buyer.decisions import Decisions
from arena_kit.buyer.interpreter import interpret
from arena_kit.buyer.model import Model, model_from_env, openai_chat
from arena_kit.buyer.prompts import INTERPRET


def test_role_profiles_keep_priority_high_and_fallback_effort_separate(monkeypatch):
    for key, value in {
        "LLM_MODEL": "gpt-6.1-sol",
        "LLM_FALLBACK_MODEL": "gpt-6-luna",
        "LLM_PROVIDER": "openai",
        "LLM_BASE_URL": "https://api.openai.com/v1",
        "LLM_TURN": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LLM_EFFORT", raising=False)
    profiles = model_from_env().configuration()
    for role in ("preparation", "interpretation", "critic"):
        assert profiles[role][0]["model"] == "gpt-6.1-sol" and profiles[role][0]["effort"] == "high"
        assert profiles[role][0]["service_tier"] == "priority"
        assert profiles[role][1]["model"] == "gpt-6-luna" and profiles[role][1]["effort"] == "low"
    assert profiles["communication"][0]["effort"] == "low"
    assert "key" not in json.dumps(profiles).lower()


def test_slow_primary_reserves_fallback_and_late_result_is_isolated(monkeypatch):
    monkeypatch.setattr(model_mod, "FALLBACK_MIN_S", 0.02)
    release = threading.Event()
    m = Model(
        [
            ("primary", lambda s, u, t: release.wait(2) and '{"late":true}'),
            ("fallback", lambda s, u, t: '{"fast":true}'),
        ],
        fallback_reserve_s=0.15,
    )
    try:
        started = time.monotonic()
        assert m.complete("s", "u", 0.3) == '{"fast":true}'
        assert time.monotonic() - started < 0.4 and m.last[0] == "fallback"
        trace = list(m.trace)
        release.set()
        time.sleep(0.02)
        assert m.last[0] == "fallback" and m.trace == trace
    finally:
        release.set()


@pytest.mark.parametrize("primary", ["not json", '{"unexpected":1}', '{"ok":false}'])
def test_schema_failure_uses_fallback_once(primary):
    calls = []

    def first(s, u, t):
        calls.append(u)
        return primary

    m = Model([("primary", first), ("fallback", lambda s, u, t: '{"ok":true}')])
    result = m.complete("s", "u", 15, validate=lambda raw: [] if json.loads(raw) == {"ok": True} else ["schema"])
    assert result == '{"ok":true}' and len(calls) == 1 and m.last[0] == "fallback"


@pytest.mark.parametrize("error", ["reasoning_effort unsupported", "service_tier unsupported", "unknown model"])
def test_unrelated_400_never_disables_json_mode(error):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": error}})

    call = openai_chat("https://llm.test/v1", "test", "gpt-x", "high", transport=httpx.MockTransport(handle))
    with pytest.raises(httpx.HTTPStatusError):
        call("s", "u", 10)
    assert len(seen) == 1 and "response_format" in seen[0]


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "length", "message": {"content": "{}"}},
        {"finish_reason": "stop", "message": {"refusal": "No", "content": "{}"}},
    ],
)
def test_adapter_rejects_truncation_and_refusal(choice):
    call = openai_chat(
        "https://llm.test",
        "test",
        "gpt-x",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [choice]})),
    )
    assert call("s", "u", 1) is None


def test_adapter_records_actual_tier_and_token_usage():
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "service_tier": "default",
                "usage": {"completion_tokens": 3},
            },
        )

    call = openai_chat(
        "https://llm.test",
        "test",
        "gpt-x",
        "high",
        transport=httpx.MockTransport(handle),
        service_tier="priority",
        max_completion_tokens=8192,
    )
    m = Model([("gpt-x", call)])
    assert m.complete("s", "u", 1) == "{}"
    assert seen[0]["reasoning_effort"] == "high" and seen[0]["service_tier"] == "priority"
    assert seen[0]["max_completion_tokens"] == 8192
    assert m.trace[0]["returned_tier"] == "default" and m.trace[0]["usage"]["completion_tokens"] == 3


def test_decisions_does_not_inherit_chat_effort_or_priority():
    seen = []

    def handle(r):
        seen.append(json.loads(r.content))
        return httpx.Response(200, json={"answers": []})

    Decisions("https://llm.test", "test", transport=httpx.MockTransport(handle)).classify_reply("Our price stands.")
    assert "reasoning_effort" not in seen[0] and "service_tier" not in seen[0]


def test_ten_concurrent_cases_share_deadlines_with_slow_primaries(monkeypatch):
    monkeypatch.setattr(model_mod, "FALLBACK_MIN_S", 0.02)
    release = threading.Event()
    m = Model(
        [("slow", lambda s, u, t: release.wait(2) and "{}"), ("fast", lambda s, u, t: "{}")], fallback_reserve_s=0.15
    )

    def complete(i):
        end = time.monotonic() + 0.5
        result = m.complete("s", str(i), 10, deadline=end)
        return result, m.last[0], time.monotonic() <= end + 0.05

    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(complete, range(10)))
        assert all(result == "{}" and provider == "fast" and on_time for result, provider, on_time in results)
    finally:
        release.set()


def test_interpreter_requires_verbatim_evidence_and_skips_simple_messages():
    calls = []

    def answer(s, u, t):
        calls.append(u)
        return json.dumps(INTERPRET.example)

    message = {"message_id": "m", "body": "Lower price if you commit longer."}
    obs = interpret("sup:A", message, model=Model([("m", answer)]), budget_s=20)
    assert obs.source == "model" and obs.values == ("contract_months",) and len(calls) == 1
    bad = {**INTERPRET.example, "evidence": ["Supplier floor is fifty euros."]}
    assert (
        interpret("sup:A", message, model=Model([("m", lambda s, u, t: json.dumps(bad))]), budget_s=20).source
        == "rules"
    )
    calls.clear()
    assert (
        interpret("sup:A", {"body": "Our price stands."}, model=Model([("m", answer)]), budget_s=20).source == "rules"
    )
    assert not calls
