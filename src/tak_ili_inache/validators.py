from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .models import BetType, Market, Prediction, Round

BANK = 5_000
BET_COUNT = 5
MIN_STAKE, MAX_STAKE, STAKE_STEP = 500, 2_500, 50


class ValidationError(ValueError):
    pass


def validate_prediction(prediction: Prediction, round_: Round, now_msk: datetime) -> None:
    if prediction.round_id != round_.round_id:
        raise ValidationError("Прогноз относится к другому туру.")
    if now_msk >= round_.deadline_msk:
        raise ValidationError("Дедлайн тура уже прошёл.")
    if len(prediction.bets) != BET_COUNT:
        raise ValidationError("Нужно ровно 5 ставок.")
    singles = sum(bet.bet_type == BetType.SINGLE for bet in prediction.bets)
    expresses = sum(bet.bet_type == BetType.EXPRESS for bet in prediction.bets)
    if (singles, expresses) not in {(4, 1), (3, 2)}:
        raise ValidationError("Допустимы только структуры 4+1 или 3+2.")
    if sum(bet.stake for bet in prediction.bets) != BANK:
        raise ValidationError("Сумма ставок должна быть ровно 5 000.")
    fixtures = {fixture.match_id: fixture for fixture in round_.fixtures}
    used_matches: set[str] = set()
    for bet_no, bet in enumerate(prediction.bets, 1):
        if not MIN_STAKE <= bet.stake <= MAX_STAKE or bet.stake % STAKE_STEP:
            raise ValidationError(f"Ставка {bet_no}: сумма от 500 до 2 500 с шагом 50.")
        expected_events = 1 if bet.bet_type == BetType.SINGLE else None
        if expected_events and len(bet.events) != expected_events:
            raise ValidationError(f"Ставка {bet_no}: ординар содержит одно событие.")
        if bet.bet_type == BetType.EXPRESS and len(bet.events) not in {2, 3}:
            raise ValidationError(f"Ставка {bet_no}: экспресс содержит 2 или 3 события.")
        for event in bet.events:
            fixture = fixtures.get(event.match_id)
            if fixture is None:
                raise ValidationError(f"Ставка {bet_no}: неизвестный матч {event.match_id}.")
            if event.match_id in used_matches:
                raise ValidationError("Матч нельзя использовать повторно во всём купоне.")
            used_matches.add(event.match_id)
            expected_odds = fixture.odds.get(event.market)
            if expected_odds is None or event.odds_snapshot != expected_odds:
                raise ValidationError(f"Ставка {bet_no}: событие или коэффициент не соответствует линии.")
            if event.market in {Market.TB, Market.TM} and event.total_line_snapshot != fixture.total_line:
                raise ValidationError(f"Ставка {bet_no}: линия тотала должна совпадать с линией матча.")
            if event.market not in {Market.TB, Market.TM} and event.total_line_snapshot is not None:
                raise ValidationError(f"Ставка {bet_no}: total_line допустим только для ТБ/ТМ.")


def ensure_results_compatible(winning_markets: set[Market], returned_markets: set[Market] | None = None) -> None:
    returned_markets = returned_markets or set()
    if returned_markets == set(Market):
        if winning_markets:
            raise ValidationError("Полный возврат не может содержать победившие рынки.")
        return
    if not winning_markets:
        raise ValidationError("Укажите результат матча или полный возврат.")
    outcome_states = {
        Market.P1: {"p1"}, Market.X: {"x"}, Market.P2: {"p2"},
        Market.ONE_X: {"p1", "x"}, Market.X_TWO: {"x", "p2"},
    }
    selected = [outcome_states[market] for market in winning_markets if market in outcome_states]
    if selected and not set.intersection(*selected):
        raise ValidationError("Несовместимые исходы матча.")
    if {Market.TB, Market.TM} <= winning_markets:
        raise ValidationError("ТБ и ТМ не могут одновременно выиграть.")
    # A returned market is a settlement override: it can stand in for a required
    # factual winner, but scoring will still apply odds 1.00 to that event.
    factual_or_returned = winning_markets | returned_markets
    if not ({Market.TB, Market.TM} & factual_or_returned):
        raise ValidationError("Укажите ТБ/ТМ или возврат одного из этих рынков.")
    required_outcomes = {
        Market.P1: {Market.P1, Market.ONE_X},
        Market.X: {Market.X, Market.ONE_X, Market.X_TWO},
        Market.P2: {Market.P2, Market.X_TWO},
    }
    primary = [market for market in (Market.P1, Market.X, Market.P2) if market in winning_markets]
    if len(primary) > 1:
        raise ValidationError("Исход матча не может содержать несколько победителей.")
    returned_primary = [market for market in (Market.P1, Market.X, Market.P2) if market in returned_markets]
    primary_market = primary[0] if primary else (returned_primary[0] if len(returned_primary) == 1 else None)
    if primary_market is None or not required_outcomes[primary_market] <= factual_or_returned:
        raise ValidationError("Исход должен быть каноническим: П1+1Х, Х+1Х+Х2 или П2+Х2.")
