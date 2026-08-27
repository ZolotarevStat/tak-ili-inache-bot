from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tak_ili_inache.models import Bet, BetEvent, BetResult, BetType, Market, Prediction
from tak_ili_inache.scoring import score_partial_predictions, score_predictions

NOW = datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Moscow"))


def prediction(participant: str, odds: list[str]) -> Prediction:
    events = [BetEvent(f"M{i}", Market.P1, Decimal(value)) for i, value in enumerate(odds, 1)]
    bets = (
        Bet(BetType.SINGLE, 1000, (events[0],)),
        Bet(BetType.SINGLE, 1000, (events[1],)),
        Bet(BetType.SINGLE, 1000, (events[2],)),
        Bet(BetType.SINGLE, 500, (events[3],)),
        Bet(BetType.EXPRESS, 1500, (events[4], events[5])),
    )
    return Prediction("R1", participant, bets, NOW)


class ScoringGoldenTests(unittest.TestCase):
    def test_partial_scoring_keeps_live_bets_pending_and_settles_known_losses(self) -> None:
        live = Prediction(
            "R1",
            "live",
            (
                Bet(BetType.SINGLE, 1000, (BetEvent("M1", Market.P1, Decimal("2.00")),)),
                Bet(
                    BetType.EXPRESS,
                    1000,
                    (
                        BetEvent("M2", Market.P1, Decimal("1.50")),
                        BetEvent("M3", Market.P1, Decimal("2.00")),
                    ),
                ),
            ),
            NOW,
        )
        lost = Prediction(
            "R1",
            "lost",
            (
                Bet(
                    BetType.EXPRESS,
                    1000,
                    (
                        BetEvent("M1", Market.P2, Decimal("3.00")),
                        BetEvent("M9", Market.P1, Decimal("2.00")),
                    ),
                ),
            ),
            NOW,
        )
        board = score_partial_predictions(
            [live, lost],
            [
                BetResult("M1", frozenset({Market.P1})),
                BetResult("M2", returned_markets=frozenset({Market.P1})),
            ],
        )
        by_id = {row.participant_id: row for row in board}
        self.assertEqual((by_id["live"].realized_payout, by_id["live"].settled_bets, by_id["live"].pending_bets), (2000, 1, 1))
        self.assertEqual((by_id["lost"].realized_payout, by_id["lost"].settled_bets, by_id["lost"].pending_bets), (0, 1, 0))

    def test_complete_partial_board_matches_final_gross_and_ranks(self) -> None:
        predictions = [
            prediction("a", ["1.77", "1.20", "2.00", "1.50", "1.50", "2.00"]),
            prediction("b", ["1.50"] * 6),
        ]
        results = [BetResult(f"M{i}", frozenset({Market.P1})) for i in range(1, 7)]
        partial = score_partial_predictions(predictions, results)
        _, final = score_predictions(predictions, results)
        self.assertEqual(
            [(row.rank, row.participant_id, row.realized_payout) for row in partial],
            [(row.rank, row.participant_id, row.gross_payout) for row in final],
        )

    def test_partial_returns_and_equal_payouts_keep_equal_places(self) -> None:
        first = Prediction(
            "R1", "a",
            (Bet(BetType.EXPRESS, 1000, (
                BetEvent("M1", Market.P1, Decimal("4.00")),
                BetEvent("M2", Market.P1, Decimal("2.00")),
            )),), NOW,
        )
        second = Prediction(
            "R1", "b",
            (Bet(BetType.SINGLE, 1000, (BetEvent("M3", Market.P1, Decimal("1.00")),)),), NOW,
        )
        third = Prediction(
            "R1", "c",
            (Bet(BetType.SINGLE, 1000, (BetEvent("M9", Market.P1, Decimal("2.00")),)),), NOW,
        )
        board = score_partial_predictions(
            [first, second, third],
            [
                BetResult("M1", returned_markets=frozenset({Market.P1})),
                BetResult("M3", returned_markets=frozenset({Market.P1})),
            ],
        )
        self.assertEqual(
            [(row.participant_id, row.rank, row.realized_payout, row.pending_bets) for row in board],
            [("b", 1, 1000, 0), ("a", 2, 0, 1), ("c", 2, 0, 1)],
        )

    def test_every_market_can_be_returned_in_single_and_express(self) -> None:
        for market in Market:
            event = BetEvent("M1", market, Decimal("2.00"))
            other = BetEvent("M2", Market.P1, Decimal("1.50"))
            single = Prediction("R1", f"single-{market.value}", (Bet(BetType.SINGLE, 1000, (event,)),), NOW)
            express = Prediction("R1", f"express-{market.value}", (Bet(BetType.EXPRESS, 1000, (event, other)),), NOW)
            results = [BetResult("M1", returned_markets=frozenset({market})), BetResult("M2", frozenset({Market.P1}))]
            scored, _ = score_predictions([single, express], results)
            self.assertEqual([row.gross_payout for row in scored], [1000, 1500], market.value)
    def test_golden_payouts_round_each_bet_half_up(self) -> None:
        p = prediction("p1", ["1.77", "1.2345", "2.00", "1.50", "1.50", "2.00"])
        results = [BetResult(f"M{i}", frozenset({Market.P1})) for i in range(1, 7)]
        scored, board = score_predictions([p], results)
        self.assertEqual([row.gross_payout for row in scored], [1770, 1235, 2000, 750, 4500])
        self.assertEqual(board[0].gross_payout, 10255)
        self.assertEqual(board[0].net_result, 5255)

    def test_lost_express_and_return_are_scored_correctly(self) -> None:
        p = prediction("p1", ["1.50"] * 6)
        results = [BetResult(f"M{i}", frozenset({Market.P1})) for i in range(1, 5)] + [
            BetResult("M5", returned_markets=frozenset({Market.P1})),
            BetResult("M6", frozenset()),
        ]
        scored, _ = score_predictions([p], results)
        self.assertEqual(scored[-1].combined_odds, Decimal("1.50"))
        self.assertFalse(scored[-1].is_win)
        self.assertEqual(scored[-1].gross_payout, 0)
        returned = Bet(BetType.SINGLE, 1000, (BetEvent("M7", Market.P1, Decimal("2.35")),))
        p_return = Prediction("R1", "p2", (returned,) * 5, NOW)
        # Direct scoring permits raw domain rows; coupon validation is deliberately separate.
        returned_scores, _ = score_predictions([p_return], [BetResult("M7", returned_markets=frozenset({Market.P1}))])
        self.assertEqual(returned_scores[0].gross_payout, 1000)

    def test_competition_ranking_keeps_equal_places(self) -> None:
        p1, p2, p3 = prediction("a", ["1.00"] * 6), prediction("b", ["1.00"] * 6), prediction("c", ["1.00"] * 5 + ["0.50"])
        results = [BetResult(f"M{i}", frozenset({Market.P1})) for i in range(1, 7)]
        # c has a lower express coefficient, so it follows the shared first place.
        _, board = score_predictions([p1, p2, p3], results)
        self.assertEqual([(row.participant_id, row.rank) for row in board], [("a", 1), ("b", 1), ("c", 3)])
