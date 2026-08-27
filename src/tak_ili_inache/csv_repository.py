from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from .models import AdminGrant, Bet, BetEvent, BetResult, BetType, Fixture, Market, Participant, Prediction, Round
from .repository import Repository

MOSCOW = ZoneInfo("Europe/Moscow")
_ADMIN_GRANT_FIELDS = {
    "event_id", "grant_event_id", "telegram_id", "participant_id", "action", "granted_by", "granted_at", "revoked_by", "revoked_at", "active"
}
_PREVIOUS_ADMIN_GRANT_FIELDS = _ADMIN_GRANT_FIELDS - {"grant_event_id"}
_LEGACY_ADMIN_GRANT_FIELDS = _PREVIOUS_ADMIN_GRANT_FIELDS - {"event_id"}


class CsvRepository(Repository):
    """Single-writer CSV storage: raw history is append-only; latest is a replaceable view."""
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.directory / ".repository.lock"
        self._before_replace = None  # test-only crash injection; never configured by runtime
        self._recover()

    def save_round(self, round_: Round, actor_id: str = "", imported_at: str = "") -> None:
        self._write_round(round_, actor_id, imported_at, "round_activated")

    def replace_round(self, round_: Round, actor_id: str = "", imported_at: str = "") -> None:
        self._write_round(round_, actor_id, imported_at, "round_replaced")

    def _write_round(self, round_: Round, actor_id: str, imported_at: str, audit_action: str) -> None:
        with self._locked():
            # Fixtures are append-only by round; rounds.csv is the commit marker.
            existing_rounds = self._read_rows("rounds.csv")
            if audit_action == "round_replaced":
                previous = next((item for item in existing_rounds if item.get("round_id") == round_.round_id), None)
                if not previous or previous.get("status", "active") != "active":
                    raise ValueError("Only the active matching round may be replaced.")
            fixture_rows = [item for item in self._read_rows("fixtures.csv") if item.get("round_id") != round_.round_id]
            fixture_rows += [self._fixture_row(item) for item in round_.fixtures]
            rounds = [item for item in existing_rounds if item.get("round_id") != round_.round_id]
            if audit_action == "round_activated" and any(item.get("status", "active") == "active" for item in rounds):
                raise ValueError("Active round must be closed before activating a new round.")
            rounds.append(self._round_row(round_))
            self._write_rows("fixtures.csv", fixture_rows)
            self._write_rows("rounds.csv", rounds)
            self._append_audit(audit_action, round_.round_id, actor_id, imported_at, round_.checksum)

    def close_round(self, round_id: str, actor_id: str = "", closed_at: str = "") -> bool:
        with self._locked():
            rows = self._read_rows("rounds.csv")
            found = next((item for item in rows if item.get("round_id") == round_id), None)
            if not found or found.get("status", "active") != "active":
                return False
            intent = self._close_intent(round_id, found["checksum"], actor_id, closed_at)
            intents = self._read_rows("close_intents.csv")
            duplicate = next((item for item in intents if item == intent), None)
            if duplicate is None:
                intents.append(intent)
                # The durable intent is written before the round commit.  If a
                # process dies at either later replace boundary, constructor
                # recovery converges to closed + one matching audit entry.
                self._write_rows("close_intents.csv", intents)
            self._commit_close_intent(intent)
            return True

    def round_status(self, round_id: str) -> str | None:
        row = next((item for item in self._read_rows("rounds.csv") if item.get("round_id") == round_id), None)
        return row.get("status", "active") if row else None

    def mark_round_scored(self, round_id: str, scored_at: str = "") -> None:
        with self._locked():
            rows = self._read_rows("rounds.csv")
            for item in rows:
                if item.get("round_id") == round_id:
                    item["scored_at"] = scored_at or "1"
            self._write_rows("rounds.csv", rows)

    def round_scored(self, round_id: str) -> bool:
        row = next((item for item in self._read_rows("rounds.csv") if item.get("round_id") == round_id), None)
        return bool(row and row.get("scored_at"))

    def get_round(self, round_id: str) -> Round | None:
        rows = self._read_rows("rounds.csv")
        row = next((item for item in rows if item["round_id"] == round_id), None)
        if not row:
            return None
        return self._round_from_row(row)

    def get_active_round(self) -> Round | None:
        rows = self._read_rows("rounds.csv")
        row = next((item for item in rows if item.get("status", "active") == "active"), None)
        return self._round_from_row(row) if row else None

    def save_prediction(self, prediction: Prediction, update_id: str = "") -> bool:
        with self._locked():
            raw = self._read_rows("submissions_raw.csv")
            latest = self._read_rows("submissions_latest.csv")
            replay = next((item for item in raw if update_id and item.get("update_id") == update_id), None)
            row = replay or self._prediction_row(prediction, update_id)
            if replay is None:
                raw.append(row)
            latest = [item for item in latest if not (item["round_id"] == prediction.round_id and item["participant_id"] == prediction.participant_id)]
            latest.append(row)
            if replay is None:
                self._write_rows("submissions_raw.csv", raw)
            self._write_rows("submissions_latest.csv", latest)
            return replay is None

    def get_prediction(self, round_id: str, participant_id: str) -> Prediction | None:
        row = next((item for item in self._read_rows("submissions_latest.csv") if item["round_id"] == round_id and item["participant_id"] == participant_id), None)
        return self._prediction_from_row(row) if row else None

    def raw_predictions(self) -> tuple[Prediction, ...]:
        return tuple(self._prediction_from_row(row) for row in self._read_rows("submissions_raw.csv"))

    def register_participant(self, telegram_id: str, display_name: str) -> Participant:
        with self._locked():
            rows = self._read_rows("participants.csv")
            row = next((item for item in rows if item["telegram_id"] == telegram_id), None)
            if row:
                return Participant(row["participant_id"], telegram_id, row["display_name"])
            participant = Participant(f"tg:{telegram_id}", telegram_id, display_name or f"Участник {telegram_id}")
            rows.append({"participant_id": participant.participant_id, "telegram_id": telegram_id, "display_name": participant.display_name})
            self._write_rows("participants.csv", rows)
            return participant

    def get_participant(self, telegram_id: str) -> Participant | None:
        row = next((item for item in self._read_rows("participants.csv") if item["telegram_id"] == telegram_id), None)
        return Participant(row["participant_id"], row["telegram_id"], row["display_name"]) if row else None

    def latest_predictions(self, round_id: str) -> tuple[Prediction, ...]:
        return tuple(self._prediction_from_row(item) for item in self._read_rows("submissions_latest.csv") if item["round_id"] == round_id)

    def participants(self) -> tuple[Participant, ...]:
        return tuple(Participant(item["participant_id"], item["telegram_id"], item["display_name"]) for item in self._read_rows("participants.csv"))

    def active_admin_grants(self) -> tuple[AdminGrant, ...]:
        participants = {row["telegram_id"]: row["participant_id"] for row in self._read_rows("participants.csv")}
        current = self._validated_admin_grant_rows(self._read_admin_grant_rows(), participants)
        return tuple(
            AdminGrant(row["telegram_id"], row["participant_id"], row["granted_by"], row["granted_at"], row["event_id"])
            for row in current.values()
            if row["active"] == "true"
        )

    def admin_grant_revision(self, telegram_id: str) -> str:
        participants = {row["telegram_id"]: row["participant_id"] for row in self._read_rows("participants.csv")}
        current = self._validated_admin_grant_rows(self._read_admin_grant_rows(), participants)
        return current.get(telegram_id, {}).get("event_id", "none")

    def grant_admin(self, telegram_id: str, participant_id: str, granted_by: str, granted_at: str, expected_revision: str) -> bool:
        with self._locked():
            participants = {row["telegram_id"]: row["participant_id"] for row in self._read_rows("participants.csv")}
            if participants.get(telegram_id) != participant_id:
                raise ValueError("Admin grant target is not a registered participant.")
            rows = self._read_admin_grant_rows()
            current = self._validated_admin_grant_rows(rows, participants)
            previous = current.get(telegram_id)
            if (previous.get("event_id") if previous else "none") != expected_revision or (previous and previous["active"] == "true"):
                return False
            event_id = secrets.token_hex(16)
            rows.append(
                {
                    "event_id": event_id,
                    "grant_event_id": event_id,
                    "telegram_id": telegram_id,
                    "participant_id": participant_id,
                    "action": "grant",
                    "granted_by": granted_by,
                    "granted_at": granted_at,
                    "revoked_by": "",
                    "revoked_at": "",
                    "active": "true",
                }
            )
            self._write_rows("admin_grants.csv", rows)
            return True

    def revoke_admin(self, telegram_id: str, revoked_by: str, revoked_at: str, expected_revision: str) -> bool:
        with self._locked():
            rows = self._read_admin_grant_rows()
            participants = {row["telegram_id"]: row["participant_id"] for row in self._read_rows("participants.csv")}
            current = self._validated_admin_grant_rows(rows, participants)
            previous = current.get(telegram_id)
            if not previous or previous.get("event_id") != expected_revision or previous.get("active") != "true":
                return False
            rows.append(
                {
                    "event_id": secrets.token_hex(16),
                    "grant_event_id": previous["event_id"],
                    "telegram_id": telegram_id,
                    "participant_id": previous["participant_id"],
                    "action": "revoke",
                    "granted_by": previous["granted_by"],
                    "granted_at": previous["granted_at"],
                    "revoked_by": revoked_by,
                    "revoked_at": revoked_at,
                    "active": "false",
                }
            )
            self._write_rows("admin_grants.csv", rows)
            return True

    def save_result(self, result: BetResult, round_id: str | None = None) -> None:
        with self._locked():
            scoped_round = round_id or (self.get_active_round().round_id if self.get_active_round() else "")
            if not scoped_round:
                raise ValueError("Round is required for result persistence.")
            rows = self._read_rows("match_results.csv")
            row = {"round_id": scoped_round, "match_id": result.match_id, "winning_markets": json.dumps(sorted(item.value for item in result.winning_markets), ensure_ascii=False), "returned_markets": json.dumps(sorted(item.value for item in result.returned_markets), ensure_ascii=False)}
            rows = [item for item in rows if not (item.get("round_id") == scoped_round and item["match_id"] == result.match_id)] + [row]
            self._write_rows("match_results.csv", rows)

    def results(self, round_id: str) -> tuple[BetResult, ...]:
        round_ = self.get_round(round_id)
        valid_ids = {item.match_id for item in round_.fixtures} if round_ else set()
        return tuple(BetResult(item["match_id"], frozenset(Market(value) for value in json.loads(item["winning_markets"])), frozenset(Market(value) for value in json.loads(item["returned_markets"]))) for item in self._read_rows("match_results.csv") if item.get("round_id") == round_id and item["match_id"] in valid_ids)

    def raw_result_rows(self) -> list[dict[str, str]]:
        return self._read_rows("match_results.csv")

    def operation_done(self, operation_key: str) -> bool:
        return any(row["operation_key"] == operation_key and row.get("status") == "done" for row in self._read_rows("operations.csv"))

    def mark_operation_done(self, operation_key: str) -> None:
        with self._locked():
            rows = self._read_rows("operations.csv")
            rows = [row for row in rows if row["operation_key"] != operation_key]
            rows.append({"operation_key": operation_key, "status": "done"})
            self._write_rows("operations.csv", rows)

    def begin_operation(self, operation_key: str) -> str:
        with self._locked():
            rows = self._read_rows("operations.csv")
            current = next((row.get("status", "done") for row in rows if row["operation_key"] == operation_key), "")
            if current:
                return current
            rows.append({"operation_key": operation_key, "status": "pending"})
            self._write_rows("operations.csv", rows)
            return "new"

    def pending_operations(self) -> tuple[str, ...]:
        return tuple(row["operation_key"] for row in self._read_rows("operations.csv") if row.get("status") == "pending")

    def resolve_operation(self, operation_key: str, delivered: bool) -> None:
        with self._locked():
            rows = self._read_rows("operations.csv")
            if delivered:
                rows = [row for row in rows if row["operation_key"] != operation_key]
                rows.append({"operation_key": operation_key, "status": "done"})
            else:
                rows = [row for row in rows if row["operation_key"] != operation_key]
            self._write_rows("operations.csv", rows)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl
        with self._lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _recover(self) -> None:
        with self._locked():
            raw = self._read_rows("submissions_raw.csv")
            expected_by_key: dict[tuple[str, str], dict[str, str]] = {}
            for row in raw:
                expected_by_key[(row["round_id"], row["participant_id"])] = row
            expected = list(expected_by_key.values())
            if self._read_rows("submissions_latest.csv") != expected:
                self._write_rows("submissions_latest.csv", expected)
            self._recover_admin_grant_log()
            rounds = self._read_rows("rounds.csv")
            migrated_rounds = []
            for row in rounds:
                migrated = dict(row)
                migrated.setdefault("status", "active")
                migrated.setdefault("closed_at", "")
                migrated.setdefault("scored_at", "")
                migrated_rounds.append(migrated)
            if migrated_rounds != rounds:
                self._write_rows("rounds.csv", migrated_rounds)
            # Legacy result rows belonged to the only historically active round.
            result_rows = self._read_rows("match_results.csv")
            if result_rows and any("round_id" not in item for item in result_rows):
                active_rows = [item for item in migrated_rounds if item.get("status") == "active"]
                if len(active_rows) != 1:
                    raise ValueError("Cannot safely migrate unscoped result rows.")
                for item in result_rows:
                    item.setdefault("round_id", active_rows[0]["round_id"])
                self._write_rows("match_results.csv", result_rows)
            if migrated_rounds:
                round_row = migrated_rounds[0]
                audits = self._read_rows("audit_log.csv")
                if not any(item["action"] == "round_activated" and item["round_id"] == round_row["round_id"] and item["checksum"] == round_row["checksum"] for item in audits):
                    self._append_audit("round_activated_recovered", round_row["round_id"], "", "", round_row["checksum"])
            self._recover_close_intents()

    def _recover_admin_grant_log(self) -> None:
        rows = self._read_admin_grant_rows()
        if not rows:
            return
        participants = {row["telegram_id"]: row["participant_id"] for row in self._read_rows("participants.csv")}
        if all(set(row) == _LEGACY_ADMIN_GRANT_FIELDS for row in rows):
            migrated = self._migrate_admin_grant_rows(rows, add_event_ids=True)
            self._validated_admin_grant_rows(migrated, participants)
            self._write_rows("admin_grants.csv", migrated)
            return
        if all(set(row) == _PREVIOUS_ADMIN_GRANT_FIELDS for row in rows):
            migrated = self._migrate_admin_grant_rows(rows, add_event_ids=False)
            self._validated_admin_grant_rows(migrated, participants)
            self._write_rows("admin_grants.csv", migrated)
            return
        self._validated_admin_grant_rows(rows, participants)

    @staticmethod
    def _migrate_admin_grant_rows(rows: list[dict[str, str]], *, add_event_ids: bool) -> list[dict[str, str]]:
        """Upgrade historical logs by pinning every revoke to its grant event."""
        current: dict[str, dict[str, str]] = {}
        migrated: list[dict[str, str]] = []
        for index, original in enumerate(rows):
            row = dict(original)
            if add_event_ids:
                seed = json.dumps(["legacy-admin-grant-v1", index, original], ensure_ascii=False, sort_keys=True).encode("utf-8")
                row["event_id"] = "legacy-" + hashlib.sha256(seed).hexdigest()[:24]
            previous = current.get(row.get("telegram_id", ""))
            row["grant_event_id"] = row["event_id"] if row.get("action") == "grant" else (previous or {}).get("event_id", "")
            migrated.append(row)
            current[row.get("telegram_id", "")] = row
        return migrated

    @staticmethod
    def _close_intent(round_id: str, checksum: str, actor_id: str, closed_at: str) -> dict[str, str]:
        return {"round_id": round_id, "checksum": checksum, "actor_id": actor_id, "closed_at": closed_at}

    def _recover_close_intents(self) -> None:
        """Finish only durable close intents; no best-effort deletion is allowed."""
        intents = self._read_rows("close_intents.csv")
        if not intents:
            return
        required = {"round_id", "checksum", "actor_id", "closed_at"}
        if any(set(item) != required or not item["round_id"] or not item["checksum"] for item in intents):
            raise ValueError("Invalid close recovery intent.")
        unique = {(item["round_id"], item["checksum"], item["actor_id"], item["closed_at"]) for item in intents}
        if len(unique) != len(intents):
            raise ValueError("Duplicate close recovery intent.")
        for intent in intents:
            self._commit_close_intent(intent)

    def _commit_close_intent(self, intent: dict[str, str]) -> None:
        """Commit a recorded close exactly once; caller holds repository lock."""
        rows = self._read_rows("rounds.csv")
        found = next((item for item in rows if item.get("round_id") == intent["round_id"]), None)
        if not found or found.get("checksum") != intent["checksum"]:
            raise ValueError("Close recovery round revision does not match intent.")
        if found.get("status", "active") == "active":
            for item in rows:
                if item.get("round_id") == intent["round_id"]:
                    item["status"] = "closed"
                    item["closed_at"] = intent["closed_at"]
            self._write_rows("rounds.csv", rows)
        elif found.get("status") != "closed":
            raise ValueError("Close recovery found unsupported round status.")
        if not self._has_audit("round_closed", intent["round_id"], intent["checksum"]):
            self._append_audit("round_closed", intent["round_id"], intent["actor_id"], intent["closed_at"], intent["checksum"])
        intents = self._read_rows("close_intents.csv")
        remaining = [item for item in intents if item != intent]
        if len(remaining) == len(intents):
            raise ValueError("Close recovery intent disappeared before commit.")
        self._write_rows("close_intents.csv", remaining)

    def _has_audit(self, action: str, round_id: str, checksum: str) -> bool:
        return any(
            item.get("action") == action
            and item.get("round_id") == round_id
            and item.get("checksum") == checksum
            for item in self._read_rows("audit_log.csv")
        )

    def _append_audit(self, action: str, round_id: str, actor_id: str, timestamp: str, checksum: str) -> None:
        rows = self._read_rows("audit_log.csv")
        rows.append({"action": action, "round_id": round_id, "actor_id": actor_id, "timestamp": timestamp, "checksum": checksum, "line_version": checksum[:12]})
        self._write_rows("audit_log.csv", rows)

    @staticmethod
    def _validated_admin_grant_rows(
        rows: list[dict[str, str]], participants: dict[str, str]
    ) -> dict[str, dict[str, str]]:
        """Validate the entire append-only permission state machine.

        The last row alone is insufficient: an invalid historical transition
        could otherwise be used to make a later active row appear trusted.
        """
        current: dict[str, dict[str, str]] = {}
        event_ids: set[str] = set()
        for row in rows:
            if set(row) != _ADMIN_GRANT_FIELDS:
                raise ValueError("Invalid admin grant row.")
            event_id = row["event_id"]
            telegram_id = row["telegram_id"]
            participant_id = row["participant_id"]
            action = row["action"]
            if not event_id or event_id in event_ids or not telegram_id or not participant_id:
                raise ValueError("Invalid admin grant event identity.")
            event_ids.add(event_id)
            if participants.get(telegram_id) != participant_id:
                raise ValueError("Admin grant target is not a registered participant.")
            previous = current.get(telegram_id)
            if action == "grant":
                if (
                    row["active"] != "true"
                    or row["grant_event_id"] != event_id
                    or not row["granted_by"]
                    or not row["granted_at"]
                    or row["revoked_by"]
                    or row["revoked_at"]
                    or (previous is not None and previous["active"] == "true")
                ):
                    raise ValueError("Invalid admin grant transition.")
            elif action == "revoke":
                if (
                    row["active"] != "false"
                    or not row["revoked_by"]
                    or not row["revoked_at"]
                    or previous is None
                    or previous["active"] != "true"
                    or previous["action"] != "grant"
                    or row["grant_event_id"] != previous["event_id"]
                    or row["participant_id"] != previous["participant_id"]
                    or row["granted_by"] != previous["granted_by"]
                    or row["granted_at"] != previous["granted_at"]
                ):
                    raise ValueError("Invalid admin revoke transition.")
            else:
                raise ValueError("Invalid admin grant action.")
            current[telegram_id] = row
        return current

    def _read_admin_grant_rows(self) -> list[dict[str, str]]:
        return self._read_rows_with_headers(
            "admin_grants.csv", _ADMIN_GRANT_FIELDS, _PREVIOUS_ADMIN_GRANT_FIELDS, _LEGACY_ADMIN_GRANT_FIELDS
        )

    def _read_rows_with_headers(self, name: str, *allowed_headers: set[str]) -> list[dict[str, str]]:
        path = self.directory / name
        if not path.exists() or path.stat().st_size == 0:
            return []
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            header = set(reader.fieldnames or ())
            if not any(header == allowed for allowed in allowed_headers):
                raise ValueError(f"Invalid {name} header.")
            return list(reader)

    def _read_rows(self, name: str) -> list[dict[str, str]]:
        path = self.directory / name
        if not path.exists() or path.stat().st_size == 0:
            return []
        with path.open(encoding="utf-8", newline="") as source:
            return list(csv.DictReader(source))

    def _write_rows(self, name: str, rows: list[dict[str, str]]) -> None:
        path = self.directory / name
        fields = list(rows[0]) if rows else []
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=self.directory, delete=False) as target:
            temp_path = Path(target.name)
            if fields:
                writer = csv.DictWriter(target, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            target.flush()
            os.fsync(target.fileno())
        if self._before_replace:
            self._before_replace(name, temp_path)
        os.replace(temp_path, path)

    @staticmethod
    def _round_row(round_: Round) -> dict[str, str]:
        return {"round_id": round_.round_id, "deadline_msk": round_.deadline_msk.isoformat(), "checksum": round_.checksum, "line_version": round_.checksum[:12], "status": "active", "closed_at": "", "scored_at": ""}

    def _round_from_row(self, row: dict[str, str]) -> Round:
        fixtures = tuple(self._fixture_from_row(item) for item in self._read_rows("fixtures.csv") if item["round_id"] == row["round_id"])
        return Round(row["round_id"], fixtures, datetime.fromisoformat(row["deadline_msk"]).astimezone(MOSCOW), row["checksum"])

    @staticmethod
    def _fixture_row(fixture: Fixture) -> dict[str, str]:
        return {"round_id": fixture.round_id, "match_id": fixture.match_id, "kickoff_msk": fixture.kickoff_msk.isoformat(), "home_team": fixture.home_team, "away_team": fixture.away_team, "total_line": str(fixture.total_line), "odds": json.dumps({key.value: str(value) for key, value in fixture.odds.items()})}

    @staticmethod
    def _fixture_from_row(row: dict[str, str]) -> Fixture:
        return Fixture(row["round_id"], row["match_id"], datetime.fromisoformat(row["kickoff_msk"]).astimezone(MOSCOW), row["home_team"], row["away_team"], Decimal(row["total_line"]), {Market(key): Decimal(value) for key, value in json.loads(row["odds"]).items()})

    @staticmethod
    def _prediction_row(prediction: Prediction, update_id: str = "") -> dict[str, str]:
        bets = [
            {"bet_type": bet.bet_type.value, "stake": bet.stake, "events": [
                {"match_id": event.match_id, "market": event.market.value, "odds": str(event.odds_snapshot), "total_line": str(event.total_line_snapshot) if event.total_line_snapshot is not None else None}
                for event in bet.events
            ]}
            for bet in prediction.bets
        ]
        return {"round_id": prediction.round_id, "participant_id": prediction.participant_id, "submitted_at_msk": prediction.submitted_at_msk.isoformat(), "update_id": update_id, "bets": json.dumps(bets, ensure_ascii=False)}

    @staticmethod
    def _prediction_from_row(row: dict[str, str]) -> Prediction:
        bets = []
        for bet in json.loads(row["bets"]):
            events = tuple(BetEvent(item["match_id"], Market(item["market"]), Decimal(item["odds"]), Decimal(item["total_line"]) if item["total_line"] is not None else None) for item in bet["events"])
            bets.append(Bet(BetType(bet["bet_type"]), int(bet["stake"]), events))
        return Prediction(row["round_id"], row["participant_id"], tuple(bets), datetime.fromisoformat(row["submitted_at_msk"]))
