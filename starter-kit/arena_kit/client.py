"""Thin HTTP client for the arena API. Every method returns the JSON the server sends."""

import os
import re
import threading
import time
import uuid

import httpx

# Venue Wi-Fi drops. Retrying with the same move id makes the server return the reply it already
# gave instead of playing the move twice, so retries are safe even mid-negotiation.
MOVE_ID_HEADER = "X-Move-Id"
RETRY_STATUS = {429, 500, 502, 503, 504}
CALLS_PER_SECOND = 9  # the arena allows 10 per key; stay just below, across all threads
# A supplier's reply can take up to about a minute (its time does not count against your 60 s),
# and a call may wait for another call on the same session first: wait long enough for both.
TIMEOUT_S = 180.0
READ_TIMEOUT_S = 10.0  # recovery and attachments must not inherit a supplier POST's long wait
# Practice has a daily quota per team: every practice session start carries what is left in this
# header, and GET /v1/me tells where the team stands.
REMAINING_HEADER = "X-Practice-Remaining"
ME_PATH = "/me"
# A 429 with a short Retry-After is the per-second rate limit: wait and retry. Any other 429 (the
# practice quota, too many open sessions) does not go away by retrying.
MAX_RETRY_AFTER_S = 5


class ArenaError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


# What an error means for the negotiation, read from its status and the arena's words.
LATE, ENDED, ROUND_LIMIT, ALREADY_PLAYED = "LATE", "ENDED", "ROUND_LIMIT", "ALREADY_PLAYED"
INVALID, RATE, OPEN_SESSIONS, QUOTA, SERVER = "INVALID", "RATE", "OPEN_SESSIONS", "QUOTA", "SERVER"


def classify(error):
    """One of the kinds above; None for what is not about a move (a wrong key, an unknown case).
    A dropped connection is SERVER: whether the arena got the request is unknown."""
    if not isinstance(error, ArenaError):
        return SERVER if isinstance(error, httpx.TransportError) else None
    status, detail = error.status, str(error.detail).lower()
    if status == 409:
        if "round limit" in detail:
            return ROUND_LIMIT
        if "one session per case" in detail or "already played" in detail:
            return ALREADY_PLAYED
        return LATE if re.search(r"\blate\b", detail) else ENDED  # thread, session or wave over
    if status in (413, 422):  # nothing happened, no round used
        return INVALID
    if status == 429:
        return QUOTA if detail.startswith("practice limit") else OPEN_SESSIONS if "open sessions" in detail else RATE
    if status >= 500:
        return SERVER
    return ENDED if status == 403 and "not running" in detail else None


class ArenaClient:
    supports_dispatch_callback = True

    def __init__(
        self,
        base_url=None,
        api_key=None,
        *,
        retries=3,
        transport=None,
        timeout=TIMEOUT_S,
        read_timeout=READ_TIMEOUT_S,
        rate=CALLS_PER_SECOND,
    ):
        base_url = (base_url or os.environ.get("ARENA_URL", "")).rstrip("/")
        if not base_url:
            raise ValueError("set ARENA_URL (e.g. https://…) or pass base_url")
        key = api_key or os.environ.get("ARENA_API_KEY")
        headers = {"X-API-Key": key} if key else {}
        self.retries, self.rate, self._lock, self._next = retries, rate, threading.Lock(), 0.0
        self.read_timeout = min(timeout, read_timeout)
        self.practice_remaining = None  # from the last practice reply that said so
        self.http = httpx.Client(base_url=f"{base_url}/v1", headers=headers, timeout=timeout, transport=transport)

    def _pace(self):
        with self._lock:
            now = time.monotonic()
            wait, self._next = self._next - now, max(now, self._next) + 1 / self.rate
        if wait > 0:
            time.sleep(wait)

    def _call(self, method, path, body=None, params=None, raw=False, move_id=None, *, on_dispatch=None):
        # a caller's move id (persisted before the call) survives a restart; else one per call
        headers = {MOVE_ID_HEADER: move_id or uuid.uuid4().hex} if method == "POST" else {}
        for attempt in range(self.retries + 1):
            self._pace()
            try:
                if on_dispatch:
                    on_dispatch(time.monotonic())
                options = {"timeout": self.read_timeout} if method == "GET" else {}
                r = self.http.request(method, path, json=body, params=params, headers=headers, **options)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                wait = None
            else:
                wait = r.headers.get("Retry-After", "")
                short = wait.isdigit() and int(wait) <= MAX_RETRY_AFTER_S
                if r.status_code not in RETRY_STATUS or attempt == self.retries or (r.status_code == 429 and not short):
                    break
            time.sleep(float(wait) if wait and wait.isdigit() else 0.5 * 2**attempt)
        if (left := r.headers.get(REMAINING_HEADER, "")).isdigit():
            self.practice_remaining = int(left)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except (ValueError, AttributeError):
                detail = r.text
            raise ArenaError(r.status_code, detail)
        return r.content if raw else r.json()

    def register(self, team_name, event_code):
        """arena-sim only: the simulator hands out its own keys. On the arena, the organisers
        give each team its key."""
        return self._call("POST", "/register", {"team_name": team_name, "event_code": event_code})

    def me(self):
        """Your team: practice sessions used and left, when the quota resets, best score per case."""
        return self._call("GET", ME_PATH)

    def cases(self, set_="practice"):
        return self._call("GET", "/cases", params={"set": set_})

    def open_session(self, case_id):
        return self._call("POST", "/sessions", {"case_id": case_id})

    def send_message(self, session_id, supplier_id, message, move_id=None, *, on_dispatch=None):
        """Your e-mail to one supplier; returns its reply (or the result, if `action` closes).
        `move_id`: repeat a request with its id and the arena answers it once, not twice."""
        path = f"/sessions/{session_id}/threads/{supplier_id}/messages"
        return self._call("POST", path, {"message": message}, move_id=move_id, on_dispatch=on_dispatch)

    def thread(self, session_id, supplier_id):
        return self._call("GET", f"/sessions/{session_id}/threads/{supplier_id}")

    def attachment(self, session_id, attachment_id):
        """The bytes of a document a supplier attached."""
        return self._call("GET", f"/sessions/{session_id}/attachments/{attachment_id}", raw=True)

    def backoffice(self, session_id):
        return self._call("GET", f"/sessions/{session_id}/backoffice")

    def move(self, session_id, supplier_id, move):
        body = {"supplier_id": supplier_id, "move": move}
        return self._call("POST", f"/sessions/{session_id}/moves", body)

    def accept(self, session_id, offer_id):
        return self._call("POST", f"/sessions/{session_id}/accept", {"offer_id": offer_id})

    def walk_away(self, session_id):
        return self._call("POST", f"/sessions/{session_id}/walk_away")

    def session(self, session_id):
        return self._call("GET", f"/sessions/{session_id}")

    def sessions(self, set_="practice"):
        return self._call("GET", "/sessions", params={"set": set_})

    def leaderboard(self, set_="practice"):
        return self._call("GET", "/leaderboard", params={"set": set_})

    def downloads(self):
        """The data you can download: the package, and the Sunday release once it is public."""
        return self._call("GET", "/data")

    def download(self, name):
        """A data download (zip bytes), e.g. "package" or "release-sunday"."""
        return self._call("GET", f"/data/{name}.zip", raw=True)
