from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Bet, BetEvent, BetType, Market, Prediction
from tak_ili_inache.validators import ValidationError, ensure_results_compatible, validate_prediction

ROUND = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")


def event(match_no: int, market: Market = Market.P1) -> BetEvent:
    fixture = ROUND.fixtures[match_no]
    return BetEvent(fixture.match_id, market, fixture.odds[market], fixture.total_line if market in {Market.TB, Market.TM} else None)


def valid_prediction() -> Prediction:
    bets = (
        Bet(BetType.SINGLE, 1000, (event(0),)),
        Bet(BetType.SINGLE, 1000, (event(1),)),
        Bet(BetType.SINGLE, 1000, (event(2),)),
        Bet(BetType.SINGLE, 500, (event(3),)),
        Bet(BetType.EXPRESS, 1500, (event(4), event(5, Market.TB))),
    )
    return Prediction("R1", "p1", bets, ROUND.deadline_msk - timedelta(minutes=1))


class PredictionValidationTests(unittest.TestCase):
    def test_accepts_4_plus_1_and_3_plus_2(self) -> None:
        prediction = valid_prediction()
        validate_prediction(prediction, ROUND, prediction.submitted_at_msk)
        bets = prediction.bets[:3] + (
            Bet(BetType.EXPRESS, 500, (event(3), event(4))),
            Bet(BetType.EXPRESS, 1500, (event(5), event(6))),
        )
        valid = Prediction("R1", "p1", bets, prediction.submitted_at_msk)
        validate_prediction(valid, ROUND, valid.submitted_at_msk)

    def test_accepts_every_event_first_cjm_shape(self) -> None:
        submitted = ROUND.deadline_msk - timedelta(minutes=1)
        cases = (
            # 6 = four singles plus a two-event express.
            (4, ((4, 5),)),
            # 7 has both permissible decompositions.
            (4, ((4, 5, 6),)),
            (3, ((3, 4), (5, 6))),
            # 8 and 9 are 3+2 with fixed express sizes.
            (3, ((3, 4), (5, 6, 7))),
            (3, ((3, 4, 5), (6, 7, 8))),
        )
        for singles, express_indexes in cases:
            with self.subTest(singles=singles, expresses=express_indexes):
                bets = [Bet(BetType.SINGLE, 1000, (event(index),)) for index in range(singles)]
                bets += [Bet(BetType.EXPRESS, 1000, tuple(event(index) for index in indexes)) for indexes in express_indexes]
                prediction = Prediction("R1", "p1", tuple(bets), submitted)
                validate_prediction(prediction, ROUND, submitted)

    def test_rejects_structure_budget_stake_and_deadline(self) -> None:
        p = valid_prediction()
        invalid_structure = Prediction("R1", "p1", p.bets[:4] + (Bet(BetType.SINGLE, 1500, (event(4),)),), p.submitted_at_msk)
        self._reject(invalid_structure, "4\\+1")
        bad_budget = Prediction("R1", "p1", p.bets[:-1] + (Bet(BetType.EXPRESS, 1450, (event(4), event(5, Market.TB))),), p.submitted_at_msk)
        self._reject(bad_budget, "5 000")
        bad_step = Prediction("R1", "p1", (Bet(BetType.SINGLE, 1025, (event(0),)),) + p.bets[1:-1] + (Bet(BetType.EXPRESS, 1475, (event(4), event(5, Market.TB))),), p.submitted_at_msk)
        self._reject(bad_step, "шагом 50")
        self._reject(p, "прошёл", now=ROUND.deadline_msk)
        over_limit = Prediction("R1", "p1", (
            Bet(BetType.SINGLE, 500, (event(0),)), Bet(BetType.SINGLE, 500, (event(1),)),
            Bet(BetType.SINGLE, 500, (event(2),)), Bet(BetType.SINGLE, 1000, (event(3),)),
            Bet(BetType.EXPRESS, 2500, (event(4), event(5, Market.TB))),
        ), p.submitted_at_msk)
        self._reject(over_limit, "2 000")

    def test_rejects_duplicate_match_invalid_express_and_stale_odds(self) -> None:
        p = valid_prediction()
        duplicate = Prediction("R1", "p1", p.bets[:-1] + (Bet(BetType.EXPRESS, 1500, (event(0), event(5))),), p.submitted_at_msk)
        self._reject(duplicate, "повторно")
        invalid_express = Prediction("R1", "p1", p.bets[:-1] + (Bet(BetType.EXPRESS, 1500, (event(4),)),), p.submitted_at_msk)
        self._reject(invalid_express, "2 или 3")
        bad_event = BetEvent("M01", Market.P1, event(0).odds_snapshot + 1)
        stale_odds = Prediction("R1", "p1", (Bet(BetType.SINGLE, 1000, (bad_event,)),) + p.bets[1:], p.submitted_at_msk)
        self._reject(stale_odds, "коэффициент")

    def test_total_line_and_result_incompatibility_are_checked(self) -> None:
        p = valid_prediction()
        bad_total = BetEvent("M06", Market.TB, event(5, Market.TB).odds_snapshot, event(5, Market.TB).total_line_snapshot + 1)
        invalid = Prediction("R1", "p1", p.bets[:-1] + (Bet(BetType.EXPRESS, 1500, (event(4), bad_total)),), p.submitted_at_msk)
        self._reject(invalid, "линия тотала")
        with self.assertRaisesRegex(ValidationError, "Несовместимые"):
            ensure_results_compatible({Market.P1, Market.X})
        with self.assertRaisesRegex(ValidationError, "Несовместимые"):
            ensure_results_compatible({Market.P1, Market.X_TWO})
        with self.assertRaisesRegex(ValidationError, "ТБ и ТМ"):
            ensure_results_compatible({Market.TB, Market.TM})
        ensure_results_compatible({Market.P1, Market.ONE_X, Market.TB})
        ensure_results_compatible(set(), set(Market))
        with self.assertRaisesRegex(ValidationError, "каноническим"):
            ensure_results_compatible({Market.P1, Market.TB})
        ensure_results_compatible({Market.P1, Market.ONE_X, Market.TB}, {Market.P1})
        # Any individual settlement override is valid when the remaining state
        # still identifies a canonical factual result.
        for market in Market:
            winners = {Market.P1, Market.ONE_X, Market.TB}
            winners.discard(market)
            ensure_results_compatible(winners, {market})

    def _reject(self, prediction, message, now=None) -> None:
        with self.assertRaisesRegex(ValidationError, message):
            validate_prediction(prediction, ROUND, now or prediction.submitted_at_msk)
