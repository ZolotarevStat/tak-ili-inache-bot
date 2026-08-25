from __future__ import annotations

import json
import tarfile
import tempfile
from pathlib import Path

from .csv_repository import CsvRepository
from .liveness import LivenessStore
from .models import BetResult, Market
from .validators import ensure_results_compatible


def create_backup(data_dir: str | Path, archive: str | Path) -> Path:
    """Create an atomic compressed backup without credentials or derived PNGs."""
    source, destination = Path(data_dir), Path(archive)
    if not source.is_dir():
        raise ValueError("Каталог данных не найден.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    repository = CsvRepository(source)
    with repository._locked():
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tar.gz", delete=False) as target:
            temporary = Path(target.name)
        try:
            with tarfile.open(temporary, "w:gz") as bundle:
                for path in source.iterdir():
                    if path.name != ".repository.lock":
                        bundle.add(path, arcname=path.name, filter=_exclude_derived_png)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as validation_dir:
        restore_backup(destination, validation_dir)
        if not health(validation_dir)["ok"]:
            destination.unlink(missing_ok=True)
            raise ValueError("Backup не прошёл restore + health validation.")
    return destination


def _exclude_derived_png(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if Path(member.name).suffix.lower() == ".png" else member


def restore_backup(archive: str | Path, destination: str | Path) -> Path:
    source, target = Path(archive), Path(destination)
    if not source.is_file():
        raise ValueError("Файл backup не найден.")
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore разрешён только в пустой каталог данных.")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source, "r:gz") as bundle:
        members = bundle.getmembers()
        if any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
            raise ValueError("Небезопасный backup archive.")
        bundle.extractall(target, members)  # nosec B202: member names are validated above
    return target


def health(data_dir: str | Path, require_liveness: bool = False, require_delivery_health: bool = False, stale_after_seconds: int = 90, now=None) -> dict[str, object]:
    directory = Path(data_dir)
    try:
        repository = CsvRepository(directory)
        round_ = repository.get_active_round()
        liveness = LivenessStore(directory, now=now).snapshot(stale_after_seconds) if (directory / LivenessStore.filename).exists() else None
        def result(data_ok: bool) -> dict[str, object]:
            delivery_ok = bool(liveness and liveness.get("delivery_ok") is True)
            return {
                "ok": data_ok and (not require_liveness or bool(liveness and liveness["ok"])) and (not require_delivery_health or delivery_ok),
                "data_ok": data_ok,
                "data_dir": str(directory),
                "active_round": round_.round_id if round_ else None,
                "liveness": liveness,
                "delivery_ok": delivery_ok,
            }
        # Every persisted round and result belongs to one explicit scope; an
        # archived round is still data that must be safe to restore.
        round_rows = repository._read_rows("rounds.csv")
        round_ids = [row.get("round_id", "") for row in round_rows]
        if len(round_ids) != len(set(round_ids)) or sum(row.get("status", "active") == "active" for row in round_rows) > 1:
            return result(False)
        rounds = [repository.get_round(round_id) for round_id in round_ids]
        if any(item is None or not _valid_round(item) for item in rounds):
            return result(False)
        for stored_round in rounds:
            assert stored_round is not None
            raw_by_key = {(item.round_id, item.participant_id): item for item in repository.raw_predictions()}
            latest_by_key = {(item.round_id, item.participant_id): item for item in repository.latest_predictions(stored_round.round_id)}
            if {key: value for key, value in raw_by_key.items() if key[0] == stored_round.round_id} != latest_by_key:
                return result(False)
        if not _valid_result_rows(repository.raw_result_rows(), {item.round_id: item for item in rounds if item is not None}):
            return result(False)
        return result(True)
    except Exception:
        return {"ok": False, "data_ok": False, "data_dir": str(directory), "active_round": None, "liveness": None}


def health_json(data_dir: str | Path, require_liveness: bool = True, require_delivery_health: bool = False) -> str:
    return json.dumps(health(data_dir, require_liveness=require_liveness, require_delivery_health=require_delivery_health), ensure_ascii=False)


def _valid_round(round_) -> bool:
    fixtures = round_.fixtures
    return (
        11 <= len(fixtures) <= 14
        and len({item.match_id for item in fixtures}) == len(fixtures)
        and {item.round_id for item in fixtures} == {round_.round_id}
        and round_.deadline_msk == min(item.kickoff_msk for item in fixtures) - __import__("datetime").timedelta(minutes=1)
    )


def _valid_result_rows(rows: list[dict[str, str]], rounds: dict[str, object]) -> bool:
    if not rows:
        return True
    seen: set[tuple[str, str]] = set()
    try:
        for row in rows:
            match_id = row["match_id"]
            round_id = row["round_id"]
            round_ = rounds.get(round_id)
            fixture_ids = {fixture.match_id for fixture in round_.fixtures} if round_ else set()
            key = (round_id, match_id)
            if match_id not in fixture_ids or key in seen:
                return False
            seen.add(key)
            result = BetResult(
                match_id,
                frozenset(Market(item) for item in json.loads(row["winning_markets"])),
                frozenset(Market(item) for item in json.loads(row["returned_markets"])),
            )
            ensure_results_compatible(set(result.winning_markets), set(result.returned_markets))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    # Partial result entry is a normal admin state; scoring alone requires completeness.
    return True
