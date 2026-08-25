from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class LivenessStore:
    """Persist worker health metadata without Telegram identifiers or payloads."""

    filename = "worker_liveness.json"

    def __init__(self, data_dir: str | Path, now=None) -> None:
        self.path = Path(data_dir) / self.filename
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state = self._read()

    def start(self) -> None:
        self.state["process_started_at"] = _stamp(self.now())
        self.state["last_successful_update_at"] = None
        self.state["last_successful_poll_at"] = None
        self.state["handler_error_count"] = 0
        self.state["polling_error_count"] = 0
        self.state["last_error_at"] = None
        self._write()

    def successful_update(self) -> None:
        self.state["last_successful_update_at"] = _stamp(self.now())
        self._write()

    def successful_poll(self) -> None:
        self.state["last_successful_poll_at"] = _stamp(self.now())
        self._write()

    def handler_error(self) -> None:
        self._error("handler")

    def polling_error(self) -> None:
        self._error("polling")

    def reply_delivered(self) -> None:
        delivered_at = _stamp(self.now())
        # A legacy snapshot recorded the cumulative count but not when its last
        # reply error happened. Its pre-upgrade delivery timestamp cannot prove
        # recovery, so the first observed post-upgrade delivery establishes the
        # lower bound conservatively.
        if int(self.state.get("reply_error_count", 0)) > 0 and not _parse_stamp(self.state.get("last_reply_error_at")):
            self.state["last_reply_error_at"] = delivered_at
        self.state["last_reply_delivered_at"] = delivered_at
        self._write()

    def reply_error(self) -> None:
        self._error("reply")

    def snapshot(self, stale_after_seconds: int = 90) -> dict[str, object]:
        now = self.now()
        poll_age = _age(now, self.state.get("last_successful_poll_at"))
        update_age = _age(now, self.state.get("last_successful_update_at"))
        last_error = self.state.get("last_error_at")
        last_poll = self.state.get("last_successful_poll_at")
        recovered = not last_error or (last_poll is not None and str(last_poll) >= str(last_error))
        ok = bool(self.state.get("process_started_at")) and poll_age is not None and poll_age <= stale_after_seconds and recovered
        reply_error_count = int(self.state.get("reply_error_count", 0))
        last_reply_error = self.state.get("last_reply_error_at")
        last_reply_delivered = self.state.get("last_reply_delivered_at")
        delivery_ok = ok and (
            reply_error_count == 0
            or _at_or_after(last_reply_delivered, last_reply_error)
        )
        return {
            "ok": ok,
            "process_started_at": self.state.get("process_started_at"),
            "last_successful_update_at": self.state.get("last_successful_update_at"),
            "last_successful_update_age_seconds": update_age,
            "last_successful_poll_at": last_poll,
            "last_successful_poll_age_seconds": poll_age,
            "handler_error_count": int(self.state.get("handler_error_count", 0)),
            "polling_error_count": int(self.state.get("polling_error_count", 0)),
            "reply_error_count": reply_error_count,
            "last_reply_error_at": last_reply_error,
            "last_reply_delivered_at": last_reply_delivered,
            "delivery_ok": delivery_ok,
            "last_error_at": last_error,
        }

    def _error(self, kind: str) -> None:
        key = f"{kind}_error_count"
        self.state[key] = int(self.state.get(key, 0)) + 1
        error_at = _stamp(self.now())
        self.state["last_error_at"] = error_at
        if kind == "reply":
            self.state["last_reply_error_at"] = error_at
        self._write()

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, encoding="utf-8", delete=False) as target:
            temporary = Path(target.name)
            json.dump(self.state, target, ensure_ascii=False, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, self.path)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _age(now: datetime, stamp: object) -> int | None:
    parsed = _parse_stamp(stamp)
    return max(0, int((now.astimezone(timezone.utc) - parsed).total_seconds())) if parsed else None


def _at_or_after(left: object, right: object) -> bool:
    left_stamp, right_stamp = _parse_stamp(left), _parse_stamp(right)
    return bool(left_stamp and right_stamp and left_stamp >= right_stamp)


def _parse_stamp(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc)
    except ValueError:
        return None
