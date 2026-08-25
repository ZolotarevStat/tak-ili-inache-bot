"""Minimal durable state for an in-progress private prediction card."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class DraftStore:
    """Atomic JSON snapshot store; it deliberately contains no Telegram payloads."""

    filename = "drafts_runtime.json"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / self.filename
        self.lock_path = self.directory / ".drafts.lock"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._snapshots = self._read()

    def load(self, participant_id: str) -> dict | None:
        value = self._snapshots.get(participant_id)
        return dict(value) if isinstance(value, dict) and value.get("state") == "active" else None

    def save(self, participant_id: str, snapshot: dict) -> None:
        self._validate(snapshot)
        with self._locked():
            current = self._read()
            current[participant_id] = dict(snapshot)
            self._write(current)
            self._snapshots = current

    def delete(self, participant_id: str) -> None:
        with self._locked():
            current = self._read()
            current.pop(participant_id, None)
            self._write(current)
            self._snapshots = current

    def _read(self) -> dict[str, dict]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write(self, value: dict[str, dict]) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.directory, delete=False) as target:
            temporary = Path(target.name)
            json.dump(value, target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, self.path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _validate(snapshot: dict) -> None:
        legacy = {"version", "draft_id", "participant_id", "chat_id", "round_id", "state", "revision", "active_message_id", "structure", "bets", "current_events", "selected_match_id", "phase", "expresses", "express_index", "event_page", "match_page", "replacement", "return_phase", "pending_reset"}
        current = legacy | {"stake_edit", "stake_edit_index"}
        v12 = current | {"replacement_kind", "selection_slots"}
        if (set(snapshot) != legacy and set(snapshot) != current and set(snapshot) != v12) or snapshot["state"] != "active":
            raise ValueError("Invalid draft snapshot.")
        if snapshot["version"] not in {1, 2, 3}:
            raise ValueError("Unsupported draft snapshot.")
        if not isinstance(snapshot["draft_id"], str) or len(snapshot["draft_id"]) > 12:
            raise ValueError("Invalid draft identifier.")
        if not isinstance(snapshot["participant_id"], str) or not isinstance(snapshot["chat_id"], int):
            raise ValueError("Invalid draft binding.")
        if not isinstance(snapshot["revision"], int) or snapshot["revision"] < 1:
            raise ValueError("Invalid draft revision.")
