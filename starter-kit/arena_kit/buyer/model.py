"""The model adapter: one `complete(system, user, timeout_s) -> str | None` over a provider chain.

The 60 s per message is a hard limit, so every call runs in a pool thread and the caller waits at
most `timeout_s` (`result(timeout)`): a slow provider is abandoned, never waited for. The HTTP
timeout is the budget + 2 s, so an abandoned call ends soon after. No SDK and no retries hide
time. A global semaphore keeps 10 parallel cases from flooding the provider. Any failure is None:
the caller falls back to the template, never to a guess."""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ..agents.llm import parse

log = logging.getLogger("arena")
MAX_CONCURRENT = 12
FALLBACK_MIN_S = 4.0  # the fallback model only gets a call it can finish
HTTP_GRACE_S = 2.0
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_pool = ThreadPoolExecutor(max_workers=2 * MAX_CONCURRENT, thread_name_prefix="llm")


def within(fn, timeout_s, *args):
    """`fn(*args)` with a hard deadline; None on timeout, error or no free slot."""
    end = time.monotonic() + timeout_s
    if timeout_s <= 0 or not _slots.acquire(timeout=timeout_s):
        return None

    def run():
        try:
            return fn(*args)
        finally:
            _slots.release()  # an abandoned call keeps its slot until its HTTP timeout ends it

    try:
        future = _pool.submit(run)
    except RuntimeError:  # the pool is shutting down
        _slots.release()
        return None
    try:
        return future.result(timeout=max(0.0, end - time.monotonic()))
    except Exception as e:  # TimeoutError included
        log.info("model call failed: %s", type(e).__name__ if isinstance(e, TimeoutError) else e)
        return None


def parse_json(text):
    """The first JSON object in a reply (tolerates fences and chatter), else None."""
    try:
        out = parse(text or "")
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


class Model:
    def __init__(self, providers):
        """`providers`: [(name, call(system, user, timeout_s) -> str)], primary first."""
        self.providers = list(providers)
        self._last = threading.local()  # per case thread: (provider name or None, ms)

    @property
    def last(self):
        return getattr(self._last, "value", (None, 0))

    def complete(self, system, user, timeout_s):
        t0 = time.monotonic()
        end = t0 + timeout_s
        for i, (name, call) in enumerate(self.providers):
            left = end - time.monotonic()
            if left <= 0 or (i and left < FALLBACK_MIN_S):
                break
            text = within(call, left, system, user, left)
            if text:
                self._last.value = (name, int(1000 * (time.monotonic() - t0)))
                return text
        self._last.value = (None, int(1000 * (time.monotonic() - t0)))
        return None


def openai_chat(base, key, model, effort=None, transport=None):
    """An OpenAI-compatible chat/completions call. JSON mode until the endpoint refuses it (400)."""
    http = httpx.Client(transport=transport)
    url, headers = f"{base.rstrip('/')}/chat/completions", {"Authorization": f"Bearer {key}"}
    state = {"json": True}

    def call(system, user, timeout_s):
        end = time.monotonic() + timeout_s
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if effort:
            body["reasoning_effort"] = effort
        while True:
            payload = {**body, "response_format": {"type": "json_object"}} if state["json"] else body
            left = end - time.monotonic()
            r = http.post(url, json=payload, headers=headers, timeout=left + HTTP_GRACE_S)
            if r.status_code == 400 and state["json"] and end - time.monotonic() > 3:
                state["json"] = False  # no JSON mode here: ask once more without it
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    return call


def anthropic_messages(base, key, model, effort=None, transport=None):
    """The Anthropic Messages API over httpx (no SDK retries); effort as `output_config`."""
    http = httpx.Client(transport=transport)
    url = f"{base.rstrip('/')}/messages"
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}

    def call(system, user, timeout_s):
        body = {"model": model, "max_tokens": 8000, "system": system, "messages": [{"role": "user", "content": user}]}
        if effort:
            body["output_config"] = {"effort": effort}
        r = http.post(url, json=body, headers=headers, timeout=timeout_s + HTTP_GRACE_S)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")

    return call


def model_from_env():
    """LLM_PROVIDER, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_EFFORT, LLM_FALLBACK_MODEL; None when
    LLM_MODEL is unset or LLM_TURN=0 (template only)."""
    primary = os.environ.get("LLM_MODEL")
    if not primary or os.environ.get("LLM_TURN", "1") == "0":
        return None
    effort = os.environ.get("LLM_EFFORT") or None
    if os.environ.get("LLM_PROVIDER", "openai") == "anthropic":
        base = os.environ.get("LLM_BASE_URL", "https://api.anthropic.com/v1")
        key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
        make = anthropic_messages
    else:
        base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
        key, make = os.environ.get("LLM_API_KEY", ""), openai_chat
    names = dict.fromkeys(m for m in (primary, os.environ.get("LLM_FALLBACK_MODEL")) if m)
    return Model([(m, make(base, key, m, effort)) for m in names])
