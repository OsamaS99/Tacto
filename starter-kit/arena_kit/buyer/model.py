"""Role-specific models: queueing, validated fallback and work share one absolute budget.

Abandoned HTTP work retains its capacity slot and cannot return to the caller or mutate state.
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import httpx

from ..agents.llm import parse

log = logging.getLogger("arena")
MAX_CONCURRENT = 12
FALLBACK_MIN_S, FALLBACK_RESERVE_S, SEND_MARGIN_S = 4.0, 8.0, 5.0
HTTP_GRACE_S = 0.25
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_pool = ThreadPoolExecutor(max_workers=2 * MAX_CONCURRENT, thread_name_prefix="llm")


@dataclass(frozen=True, slots=True)
class RoleProfile:
    model: str
    effort: str
    service_tier: str | None
    max_completion_tokens: int


class Completion(str):
    def __new__(cls, value, *, tier=None, usage=None):
        out = super().__new__(cls, value)
        out.tier, out.usage = tier, usage or {}
        return out


def within(fn, timeout_s, *args):
    end = time.monotonic() + timeout_s
    if timeout_s <= 0 or not _slots.acquire(timeout=timeout_s):
        return None

    def run():
        try:
            return fn(*args)
        finally:
            _slots.release()

    try:
        future = _pool.submit(run)
    except RuntimeError:
        _slots.release()
        return None
    try:
        return future.result(timeout=max(0.0, end - time.monotonic()))
    except Exception as e:
        log.info("model call failed: %s", type(e).__name__)
        return None


def parse_json(text):
    try:
        out = parse(text or "")
    except (ValueError, TypeError):
        return None
    return out if isinstance(out, dict) else None


class Model:
    def __init__(self, providers, *, role_providers=None, profiles=None, fallback_reserve_s=FALLBACK_RESERVE_S):
        self.providers = list(providers)
        self.role_providers, self.profiles = role_providers or {}, profiles or {}
        self.fallback_reserve_s = fallback_reserve_s
        self._last = threading.local()

    @property
    def last(self):
        return getattr(self._last, "value", (None, 0))

    @property
    def trace(self):
        return getattr(self._last, "trace", [])

    def configuration(self):
        return {role: [asdict(p) for p in profiles] for role, profiles in self.profiles.items()}

    def complete(self, system, user, timeout_s, *, role="communication", deadline=None, validate=None):
        """Validated JSON; deadline excludes dispatch margin. No extra primary repair chain."""
        t0 = time.monotonic()
        end = min(t0 + max(0.0, timeout_s), deadline if deadline is not None else float("inf"))
        providers = self.role_providers.get(role, self.providers)
        attempts = []
        self._last.value, self._last.trace = (None, 0), attempts
        for i, (name, call) in enumerate(providers):
            left = end - time.monotonic()
            if left <= 0 or (i and left < FALLBACK_MIN_S):
                break
            if i == 0 and len(providers) > 1:
                if left < self.fallback_reserve_s + FALLBACK_MIN_S:
                    continue  # spend a short budget on the faster fallback
                allotted = left - self.fallback_reserve_s
            else:
                allotted = left
            attempt_end, started = time.monotonic() + allotted, []

            def invoke(call=call, attempt_end=attempt_end, started=started):
                started.append(time.monotonic())
                available = attempt_end - time.monotonic()
                return call(system, user, available) if available > 0 else None

            a0 = time.monotonic()
            answer = within(invoke, allotted)
            errors = []
            if not answer:
                errors = ["provider_error_or_timeout"]
            elif time.monotonic() >= end:
                errors = ["deadline"]
            elif parse_json(answer) is None:
                errors = ["invalid_json"]
            elif validate:
                try:
                    errors = list(validate(answer) or [])
                except Exception:
                    errors = ["validation_exception"]
            if time.monotonic() >= end:
                errors = ["deadline"]
            profiles = self.profiles.get(role, [])
            profile = profiles[i] if i < len(profiles) else None
            attempt = {
                "role": role,
                "model": name,
                "effort": profile.effort if profile else None,
                "requested_tier": profile.service_tier if profile else None,
                "returned_tier": getattr(answer, "tier", None),
                "queue_ms": round(1000 * ((started[0] if started else time.monotonic()) - a0)),
                "attempt_ms": round(1000 * (time.monotonic() - a0)),
                "validation": "passed" if not errors else "rejected",
                "fallback_reason": errors[0] if errors else None,
                "usage": getattr(answer, "usage", {}),
            }
            attempts.append(attempt)
            log.info("model %s", json.dumps(attempt))
            if not errors:
                self._last.value = (name, round(1000 * (time.monotonic() - t0)))
                return answer
        self._last.value = (None, round(1000 * (time.monotonic() - t0)))
        return None


def _unsupported_format(response):
    try:
        detail = json.dumps(response.json().get("error", {})).lower()
    except (ValueError, AttributeError):
        return False
    return "response_format" in detail and any(x in detail for x in ("unsupported", "not supported"))


def openai_chat(base, key, model, effort=None, transport=None, *, service_tier=None, max_completion_tokens=8192):
    http = httpx.Client(transport=transport)
    url, headers = f"{base.rstrip('/')}/chat/completions", {"Authorization": f"Bearer {key}"}
    state, lock = {"json": True}, threading.Lock()

    def call(system, user, timeout_s):
        end = time.monotonic() + timeout_s
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": max_completion_tokens,
        }
        if effort:
            body["reasoning_effort"] = effort
        if service_tier:
            body["service_tier"] = service_tier
        while (left := end - time.monotonic()) > 0:
            with lock:
                json_mode = state["json"]
            payload = {**body, "response_format": {"type": "json_object"}} if json_mode else body
            r = http.post(url, json=payload, headers=headers, timeout=left + HTTP_GRACE_S)
            if r.status_code == 400 and json_mode and _unsupported_format(r) and end - time.monotonic() > 3:
                with lock:
                    state["json"] = False
                continue
            r.raise_for_status()
            data = r.json()
            choice, message = data["choices"][0], data["choices"][0]["message"]
            if choice.get("finish_reason") not in (None, "stop") or message.get("refusal"):
                return None
            content = message.get("content")
            return Completion(content, tier=data.get("service_tier"), usage=data.get("usage")) if content else None
        return None

    return call


def anthropic_messages(base, key, model, effort=None, transport=None, *, max_completion_tokens=8192):
    http = httpx.Client(transport=transport)
    url = f"{base.rstrip('/')}/messages"
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}

    def call(system, user, timeout_s):
        body = {
            "model": model,
            "max_tokens": max_completion_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if effort:
            body["output_config"] = {"effort": effort}
        r = http.post(url, json=body, headers=headers, timeout=timeout_s + HTTP_GRACE_S)
        r.raise_for_status()
        data = r.json()
        if data.get("stop_reason") in ("max_tokens", "refusal"):
            return None
        return Completion(
            "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text"), usage=data.get("usage")
        )

    return call


def model_from_env():
    """Unconfigured or LLM_TURN=0 means templates. Returned configuration never contains secrets."""
    primary = os.environ.get("LLM_MODEL")
    if not primary or os.environ.get("LLM_TURN", "1") == "0":
        return None
    provider = os.environ.get("LLM_PROVIDER", "openai")
    base = os.environ.get(
        "LLM_BASE_URL", "https://api.anthropic.com/v1" if provider == "anthropic" else "https://api.openai.com/v1"
    )
    key = os.environ.get("LLM_API_KEY") or (os.environ.get("ANTHROPIC_API_KEY", "") if provider == "anthropic" else "")
    default_tier = "priority" if provider == "openai" and urlparse(base).hostname == "api.openai.com" else None
    tier = os.environ.get("LLM_SERVICE_TIER", default_tier) or None
    fallback = os.environ.get("LLM_FALLBACK_MODEL", "gpt-6-luna" if provider == "openai" else "")
    role_providers, profiles = {}, {}
    for role, effort, limit in (
        ("preparation", "high", 8192),
        ("interpretation", "high", 8192),
        ("communication", "low", 2048),
        ("critic", "high", 16384),
    ):
        chosen_effort = os.environ.get(f"LLM_{role.upper()}_EFFORT") or (
            os.environ.get("LLM_EFFORT", effort) if role != "communication" else effort
        )
        chain = [RoleProfile(primary, chosen_effort, tier, limit)]
        if fallback and fallback != primary:
            chain.append(RoleProfile(fallback, os.environ.get("LLM_FALLBACK_EFFORT", "low"), tier, 2048))
        profiles[role] = chain
        calls = []
        for p in chain:
            if provider == "anthropic":
                call = anthropic_messages(base, key, p.model, p.effort, max_completion_tokens=p.max_completion_tokens)
            else:
                call = openai_chat(
                    base,
                    key,
                    p.model,
                    p.effort,
                    service_tier=p.service_tier,
                    max_completion_tokens=p.max_completion_tokens,
                )
            calls.append((p.model, call))
        role_providers[role] = calls
    return Model(role_providers["communication"], role_providers=role_providers, profiles=profiles)
