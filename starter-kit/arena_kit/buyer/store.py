"""What a restart needs and the arena cannot tell us: the moves we were about to send (their move
ids, so a re-send is answered once, not played twice) and a small memo of the agent. Everything
else comes back from the arena (GET session and threads). Plus one event line per move, to read a
run afterwards. Never holds a key: only what was sent, received and decided.

    runs/<set or case>/<case_id>/<session_id>.json           snapshot, replaced atomically
    runs/<set or case>/<case_id>/<session_id>.events.jsonl   append-only
"""

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .records import OutboxMove

RUNS_ENV = "BUYER_RUNS"


def _safe(name):
    return re.sub(r"[^\w.@-]", "_", str(name))


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get(RUNS_ENV) or "runs")
        self.lock = threading.Lock()  # one store may serve every case of a run
        self.dirs = {}  # session_id -> its folder

    def begin(self, session_id, case_id, group):
        """Where this session's files go: under its set (or case) and case."""
        self.dirs[session_id] = self.root / _safe(group) / _safe(case_id)

    def _dir(self, session_id):
        if session_id not in self.dirs:  # a restart: find what an earlier run wrote
            found = next(self.root.glob(f"*/*/{_safe(session_id)}.json"), None)
            if found is None:
                return self.root / "_" / "_"
            self.dirs[session_id] = found.parent
        return self.dirs[session_id]

    def _read(self, session_id):
        path = self._dir(session_id) / f"{_safe(session_id)}.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return {"session_id": session_id, "outbox": {}, "memo": {}}

    def _write(self, session_id, data):
        folder = self._dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, folder / f"{_safe(session_id)}.json")  # a crash leaves the old or the new, never half

    def put(self, move: OutboxMove):
        """Record a move (new, or its new state) - before the POST, so a crash cannot lose its id."""
        with self.lock:
            data = self._read(move.session_id)
            data["outbox"][move.move_id] = asdict(move)
            self._write(move.session_id, data)

    def load(self, session_id):
        """The moves sent (or about to be) whose answer we never saw, oldest first."""
        with self.lock:
            moves = [OutboxMove(**m) for m in self._read(session_id)["outbox"].values()]
        return sorted((m for m in moves if m.state == "pending"), key=lambda m: m.created_at)

    def memo(self, session_id, **values):
        """Merge a few agent values (e.g. the aspiration step); returns the memo."""
        with self.lock:
            data = self._read(session_id)
            data["memo"].update(values)
            if values:
                self._write(session_id, data)
            return data["memo"]

    def event(self, session_id, **fields):
        line = json.dumps({"ts": round(time.time(), 3), **fields}, ensure_ascii=False, default=str)
        with self.lock:
            folder = self._dir(session_id)
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / f"{_safe(session_id)}.events.jsonl", "a") as f:
                f.write(line + "\n")
