"""App-local persistence for normalized Spiritus session transcripts."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class SessionStore:
    """Atomic JSON transcript store used as the engine-independent record.

    OpenCode remains the conversational memory substrate. Spiritus keeps the
    normalized inputs/results as well so history remains readable across
    adapter changes and engine response-validation defects.
    """

    def __init__(self, root: Path):
        self.root = Path(root) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")
        return self.root / f"{session_id}.json"

    def load(self, session_id: str) -> list[dict]:
        path = self._path(session_id)
        with self._lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return []
            if not isinstance(payload, list):
                raise RuntimeError(f"invalid Spiritus transcript: {path}")
            return payload

    def replace(self, session_id: str, messages: list[dict]) -> None:
        path = self._path(session_id)
        body = json.dumps(messages, indent=2, ensure_ascii=False) + "\n"
        with self._lock:
            fd, temporary = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(body)
                os.replace(temporary, path)
            except BaseException:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass
                raise

    def append(self, session_id: str, messages: list[dict]) -> None:
        with self._lock:
            current = self.load(session_id)
            by_id = {message.get("id"): index for index, message in enumerate(current)}
            for message in messages:
                message_id = message.get("id")
                if message_id and message_id in by_id:
                    current[by_id[message_id]] = message
                else:
                    current.append(message)
                    if message_id:
                        by_id[message_id] = len(current) - 1
            self.replace(session_id, current)


class ApprovalAuditLog:
    """Append-only, app-local record of permission requests and decisions."""

    def __init__(self, root: Path):
        self.path = Path(root) / "approvals.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, kind: str, **fields) -> dict:
        record = {
            "time": datetime.now(UTC).isoformat(),
            "kind": kind,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def entries(self, *, session_id: str | None = None) -> list[dict]:
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
        records = [json.loads(line) for line in lines if line.strip()]
        if session_id is not None:
            records = [item for item in records if item.get("session_id") == session_id]
        return records


__all__ = ["ApprovalAuditLog", "SessionStore"]
