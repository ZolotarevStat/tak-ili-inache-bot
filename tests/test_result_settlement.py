from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Market
from tak_ili_inache.result_settlement import GoalChoice, settle_score


class ResultSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
        self.fixture = round_.fixtures[0]

    def test_home_win_and_over_are_derived(self) -> None:
        result = settle_score(self.fixture, GoalChoice(4), GoalChoice(1))
        self.assertEqual(result.winning_markets, frozenset({Market.P1, Market.ONE_X, Market.TB}))
        self.assertFalse(result.returned_markets)

    def test_draw_and_under_are_derived(self) -> None:
        result = settle_score(self.fixture, GoalChoice(1), GoalChoice(1))
        self.assertEqual(result.winning_markets, frozenset({Market.X, Market.ONE_X, Market.X_TWO, Market.TM}))

    def test_integer_total_push_returns_both_total_markets(self) -> None:
        fixture = replace(self.fixture, total_line=Decimal("2"))
        result = settle_score(fixture, GoalChoice(1), GoalChoice(1))
        self.assertEqual(result.returned_markets, frozenset({Market.TB, Market.TM}))

    def test_both_five_plus_requires_outcome(self) -> None:
        with self.assertRaises(ValueError):
            settle_score(self.fixture, GoalChoice(5, True), GoalChoice(5, True))
        result = settle_score(self.fixture, GoalChoice(5, True), GoalChoice(5, True), Market.P2)
        self.assertEqual(result.winning_markets, frozenset({Market.P2, Market.X_TWO, Market.TB}))


if __name__ == "__main__":
    unittest.main()
