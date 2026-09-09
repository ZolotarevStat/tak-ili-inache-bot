"""Fail-closed admin import for predictions received after the common deadline."""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO

from .models import Bet, BetEvent, BetType, Market, Participant, Prediction, Round
from .validators import ValidationError, validate_prediction


REQUIRED_FIELDS = {"round_id", "display_name", "bet_no", "bet_type", "stake", "match_id", "market"}


def import_late_predictions(content: bytes, round_: Round, participants: tuple[Participant, ...], now: datetime) -> tuple[Prediction, ...]:
    """Parse a CSV without Telegram IDs and accept only not-yet-started events."""
    if now < round_.deadline_msk:
        raise ValidationError("Late CSV доступен только после общего дедлайна.")
    try:
        rows = list(csv.DictReader(StringIO(content.decode("utf-8-sig"))))
    except UnicodeDecodeError as error:
        raise ValidationError("Late CSV должен быть в UTF-8.") from error
    if not rows or set(rows[0]) != REQUIRED_FIELDS:
        raise ValidationError("Late CSV должен содержать ровно колонки: " + ", ".join(sorted(REQUIRED_FIELDS)) + ".")
    people_by_name: dict[str, str] = {}
    for participant in participants:
        name = " ".join(participant.display_name.split())
        if not name or name in people_by_name:
            # The import must never choose between ambiguous people.
            continue
        people_by_name[name] = participant.participant_id
    fixtures = {item.match_id: item for item in round_.fixtures}
    grouped: dict[str, dict[int, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for line_no, row in enumerate(rows, 2):
        if set(row) != REQUIRED_FIELDS:
            raise ValidationError(f"Строка {line_no}: набор колонок не совпадает с шаблоном.")
        if row["round_id"] != round_.round_id:
            raise ValidationError(f"Строка {line_no}: указан другой тур.")
        participant_id = people_by_name.get(" ".join(row["display_name"].split()))
        if not participant_id:
            raise ValidationError(f"Строка {line_no}: участник не зарегистрирован.")
        try:
            bet_no = int(row["bet_no"])
        except ValueError as error:
            raise ValidationError(f"Строка {line_no}: bet_no должен быть целым числом.") from error
        if bet_no < 1:
            raise ValidationError(f"Строка {line_no}: bet_no должен быть больше нуля.")
        grouped[participant_id][bet_no].append(row)

    predictions: list[Prediction] = []
    for participant_id, bet_rows in grouped.items():
        bets: list[Bet] = []
        for expected_no, bet_no in enumerate(sorted(bet_rows), 1):
            if bet_no != expected_no:
                raise ValidationError(f"Участник {participant_id}: bet_no должны идти подряд с 1.")
            rows_for_bet = bet_rows[bet_no]
            types, stakes = {row["bet_type"] for row in rows_for_bet}, {row["stake"] for row in rows_for_bet}
            if len(types) != 1 or len(stakes) != 1:
                raise ValidationError(f"Участник {participant_id}, ставка {bet_no}: тип и сумма должны совпадать во всех строках ставки.")
            try:
                bet_type, stake = BetType(types.pop()), int(stakes.pop())
            except (ValueError, InvalidOperation) as error:
                raise ValidationError(f"Участник {participant_id}, ставка {bet_no}: некорректный тип или сумма.") from error
            events: list[BetEvent] = []
            for row in rows_for_bet:
                fixture = fixtures.get(row["match_id"])
                if not fixture:
                    raise ValidationError(f"Участник {participant_id}: неизвестный матч {row['match_id']}.")
                if fixture.kickoff_msk <= now:
                    raise ValidationError(f"Участник {participant_id}: матч {fixture.home_team} — {fixture.away_team} уже начался.")
                try:
                    market = Market(row["market"])
                except ValueError as error:
                    raise ValidationError(f"Участник {participant_id}: неизвестный исход {row['market']}.") from error
                odds = fixture.odds.get(market)
                if odds is None:
                    raise ValidationError(f"Участник {participant_id}: исход не соответствует линии.")
                events.append(BetEvent(fixture.match_id, market, odds, fixture.total_line if market in {Market.TB, Market.TM} else None))
            bets.append(Bet(bet_type, stake, tuple(events)))
        prediction = Prediction(round_.round_id, participant_id, tuple(bets), now)
        # Reuse every normal betting invariant but deliberately evaluate the
        # deadline guard at the last instant before it; the explicit kickoff
        # check above is the late-entry policy.
        validate_prediction(prediction, round_, round_.deadline_msk - timedelta(microseconds=1))
        predictions.append(prediction)
    return tuple(predictions)
