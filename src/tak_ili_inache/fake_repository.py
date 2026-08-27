from __future__ import annotations

import json

from .models import AdminGrant, BetResult, Participant, Prediction, Round
from .repository import Repository


class FakeRepository(Repository):
    """Append-only submissions plus the latest snapshot, for domain tests."""
    def __init__(self) -> None:
        self._rounds: dict[str, Round] = {}
        self._round_status: dict[str, str] = {}
        self._scored: set[str] = set()
        self._latest: dict[tuple[str, str], Prediction] = {}
        self._raw: list[Prediction] = []
        self._participants: dict[str, Participant] = {}
        self._results: dict[str, BetResult] = {}
        self._update_ids: set[str] = set()
        self._operations: dict[str, str] = {}
        self._admin_grants: dict[str, AdminGrant] = {}
        self._admin_grant_revisions: dict[str, str] = {}
        self._admin_grant_event_no = 0

    def save_round(self, round_: Round, actor_id: str = "", imported_at: str = "") -> None:
        active = self.get_active_round()
        if active and active.round_id != round_.round_id:
            raise ValueError("Active round must be closed before activating a new round.")
        self._rounds[round_.round_id] = round_
        self._round_status[round_.round_id] = "active"

    def replace_round(self, round_: Round, actor_id: str = "", imported_at: str = "") -> None:
        if self._round_status.get(round_.round_id) != "active":
            raise ValueError("Only the active matching round may be replaced.")
        self._rounds[round_.round_id] = round_
        self._round_status[round_.round_id] = "active"

    def close_round(self, round_id: str, actor_id: str = "", closed_at: str = "") -> bool:
        if self._round_status.get(round_id) != "active":
            return False
        self._round_status[round_id] = "closed"
        return True

    def round_status(self, round_id: str) -> str | None:
        return self._round_status.get(round_id)

    def mark_round_scored(self, round_id: str, scored_at: str = "") -> None:
        self._scored.add(round_id)

    def round_scored(self, round_id: str) -> bool:
        return round_id in self._scored

    def get_round(self, round_id: str) -> Round | None:
        return self._rounds.get(round_id)

    def save_prediction(self, prediction: Prediction, update_id: str = "") -> bool:
        if update_id and update_id in self._update_ids:
            return False
        if update_id:
            self._update_ids.add(update_id)
        self._raw.append(prediction)
        self._latest[(prediction.round_id, prediction.participant_id)] = prediction
        return True

    def get_prediction(self, round_id: str, participant_id: str) -> Prediction | None:
        return self._latest.get((round_id, participant_id))

    def raw_predictions(self) -> tuple[Prediction, ...]:
        return tuple(self._raw)

    def register_participant(self, telegram_id: str, display_name: str) -> Participant:
        participant = self._participants.get(telegram_id)
        if participant:
            return participant
        participant = Participant(f"tg:{telegram_id}", telegram_id, display_name or f"Участник {telegram_id}")
        self._participants[telegram_id] = participant
        return participant

    def get_participant(self, telegram_id: str) -> Participant | None:
        return self._participants.get(telegram_id)

    def get_active_round(self) -> Round | None:
        return next((round_ for round_id, round_ in self._rounds.items() if self._round_status.get(round_id) == "active"), None)

    def latest_predictions(self, round_id: str) -> tuple[Prediction, ...]:
        return tuple(item for (stored_round, _), item in self._latest.items() if stored_round == round_id)

    def participants(self) -> tuple[Participant, ...]:
        return tuple(self._participants.values())

    def active_admin_grants(self) -> tuple[AdminGrant, ...]:
        return tuple(self._admin_grants.values())

    def admin_grant_revision(self, telegram_id: str) -> str:
        return self._admin_grant_revisions.get(telegram_id, "none")

    def _next_admin_grant_revision(self) -> str:
        self._admin_grant_event_no += 1
        return f"event-{self._admin_grant_event_no}"

    def grant_admin(self, telegram_id: str, participant_id: str, granted_by: str, granted_at: str, expected_revision: str) -> bool:
        if self.admin_grant_revision(telegram_id) != expected_revision or telegram_id in self._admin_grants:
            return False
        revision = self._next_admin_grant_revision()
        self._admin_grant_revisions[telegram_id] = revision
        self._admin_grants[telegram_id] = AdminGrant(telegram_id, participant_id, granted_by, granted_at, revision)
        return True

    def revoke_admin(self, telegram_id: str, revoked_by: str, revoked_at: str, expected_revision: str) -> bool:
        if self.admin_grant_revision(telegram_id) != expected_revision or telegram_id not in self._admin_grants:
            return False
        self._admin_grants.pop(telegram_id)
        self._admin_grant_revisions[telegram_id] = self._next_admin_grant_revision()
        return True

    def save_result(self, result: BetResult, round_id: str | None = None) -> None:
        scoped_round = round_id or (self.get_active_round().round_id if self.get_active_round() else "")
        if not scoped_round:
            raise ValueError("Round is required for result persistence.")
        self._results[(scoped_round, result.match_id)] = result

    def results(self, round_id: str) -> tuple[BetResult, ...]:
        round_ = self.get_round(round_id)
        return tuple(self._results[(round_id, item.match_id)] for item in round_.fixtures if (round_id, item.match_id) in self._results) if round_ else ()

    def raw_result_rows(self) -> list[dict[str, str]]:
        return [{"round_id": round_id, "match_id": item.match_id, "winning_markets": json.dumps([market.value for market in item.winning_markets]), "returned_markets": json.dumps([market.value for market in item.returned_markets])} for (round_id, _), item in self._results.items()]

    def operation_done(self, operation_key: str) -> bool:
        return self._operations.get(operation_key) == "done"

    def mark_operation_done(self, operation_key: str) -> None:
        self._operations[operation_key] = "done"

    def begin_operation(self, operation_key: str) -> str:
        state = self._operations.get(operation_key)
        if state:
            return state
        self._operations[operation_key] = "pending"
        return "new"

    def pending_operations(self) -> tuple[str, ...]:
        return tuple(key for key, status in self._operations.items() if status == "pending")

    def resolve_operation(self, operation_key: str, delivered: bool) -> None:
        if delivered:
            self._operations[operation_key] = "done"
        else:
            self._operations.pop(operation_key, None)
