from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Fixture, Market, Round
from .validators import ValidationError

MOSCOW = ZoneInfo("Europe/Moscow")
REQUIRED_COLUMNS = {
    "round_id", "match_id", "kickoff_msk", "home_team", "away_team", "total_line",
    "odds_p1", "odds_x", "odds_p2", "odds_tb", "odds_tm", "odds_1x", "odds_x2",
}
ODDS_COLUMNS = {
    "odds_p1": Market.P1, "odds_x": Market.X, "odds_p2": Market.P2,
    "odds_tb": Market.TB, "odds_tm": Market.TM, "odds_1x": Market.ONE_X,
    "odds_x2": Market.X_TWO,
}


def import_fixtures(path: str | Path) -> Round:
    source = Path(path)
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValidationError("fixtures CSV должен быть в UTF-8.") from error
    rows = list(csv.DictReader(text.splitlines()))
    headers = set(rows[0]) if rows else set()
    missing = REQUIRED_COLUMNS - headers
    if missing:
        raise ValidationError(f"В CSV отсутствуют обязательные колонки: {', '.join(sorted(missing))}.")
    if not 11 <= len(rows) <= 14:
        raise ValidationError("В туре должно быть от 11 до 14 матчей.")
    fixtures = tuple(_parse_row(row, index + 2) for index, row in enumerate(rows))
    round_ids = {fixture.round_id for fixture in fixtures}
    if len(round_ids) != 1:
        raise ValidationError("Все матчи CSV должны относиться к одному round_id.")
    match_ids = [fixture.match_id for fixture in fixtures]
    if len(set(match_ids)) != len(match_ids):
        raise ValidationError("match_id не должен повторяться.")
    if len({fixture.kickoff_msk for fixture in fixtures}) == 0:
        raise ValidationError("Не найдено время начала матчей.")
    return Round(
        round_id=fixtures[0].round_id,
        fixtures=tuple(sorted(fixtures, key=lambda item: item.kickoff_msk)),
        deadline_msk=min(item.kickoff_msk for item in fixtures) - timedelta(minutes=1),
        checksum=hashlib.sha256(raw).hexdigest(),
    )


def _parse_row(row: dict[str, str], line_no: int) -> Fixture:
    def required(name: str) -> str:
        value = (row.get(name) or "").strip()
        if not value:
            raise ValidationError(f"Строка {line_no}: поле {name} обязательно.")
        return value

    round_id, match_id = required("round_id"), required("match_id")
    home, away = required("home_team"), required("away_team")
    if home == away:
        raise ValidationError(f"Строка {line_no}: команды матча должны различаться.")
    kickoff = _parse_kickoff(required("kickoff_msk"), line_no)
    total_line = _positive_decimal(required("total_line"), "total_line", line_no, strictly_gt_one=False)
    odds = {market: _positive_decimal(required(column), column, line_no) for column, market in ODDS_COLUMNS.items()}
    return Fixture(round_id, match_id, kickoff, home, away, total_line, odds)


def _parse_kickoff(value: str, line_no: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"Строка {line_no}: kickoff_msk должен быть ISO datetime.") from error
    return parsed.replace(tzinfo=MOSCOW) if parsed.tzinfo is None else parsed.astimezone(MOSCOW)


def _positive_decimal(value: str, field: str, line_no: int, strictly_gt_one: bool = True) -> Decimal:
    try:
        result = Decimal(value.replace(",", "."))
        if not result.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise ValidationError(f"Строка {line_no}: {field} должен быть десятичным числом.") from error
    if result.as_tuple().exponent < -3 or result.adjusted() > 4:
        raise ValidationError(f"Строка {line_no}: {field} вне допустимого диапазона.")
    threshold = Decimal("1") if strictly_gt_one else Decimal("0")
    if result <= threshold:
        comparator = "больше 1" if strictly_gt_one else "больше 0"
        raise ValidationError(f"Строка {line_no}: {field} должен быть {comparator}.")
    return result
