from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image

from scripts.prototype_player_cards import synthetic_data, write_demo
from tak_ili_inache.player_cards import (
    CARD_HEIGHT,
    CARD_WIDTH,
    FOOTER_TOP,
    build_player_cards,
    card_bet_boxes,
    media_batches,
    nearest_neighbours,
    prediction_similarity,
    similarity_order,
)
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Bet, BetEvent, BetType, Market, Prediction


ROOT = Path(__file__).resolve().parents[1]
ROUND = import_fixtures(ROOT / "data" / "fixtures_sample.csv")


def prediction(participant_id: str, indexes: tuple[int, ...], markets: tuple[Market, ...]) -> Prediction:
    events = []
    for fixture_index, market in zip(indexes, markets):
        fixture = ROUND.fixtures[fixture_index]
        events.append(BetEvent(
            fixture.match_id,
            market,
            fixture.odds[market],
            fixture.total_line if market in {Market.TB, Market.TM} else None,
        ))
    return Prediction(
        ROUND.round_id,
        participant_id,
        tuple(Bet(BetType.SINGLE, 1000, (event,)) for event in events),
        ROUND.deadline_msk,
    )


class PlayerCardsPrototypeTests(unittest.TestCase):
    def test_similarity_prioritizes_matches_then_exact_outcomes(self) -> None:
        base = prediction("a", (0, 1, 2), (Market.P1, Market.X, Market.P2))
        identical = prediction("b", (0, 1, 2), (Market.P1, Market.X, Market.P2))
        same_matches = prediction("c", (0, 1, 2), (Market.P2, Market.P1, Market.X))
        partial = prediction("d", (0, 1, 3), (Market.P1, Market.X, Market.TB))
        disjoint = prediction("e", (4, 5, 6), (Market.P1, Market.X, Market.P2))
        self.assertEqual(prediction_similarity(base, identical), Decimal("1.0000"))
        self.assertEqual(prediction_similarity(base, same_matches), Decimal("0.7000"))
        self.assertGreater(prediction_similarity(base, partial), Decimal("0"))
        self.assertLess(prediction_similarity(base, partial), Decimal("0.7000"))
        self.assertEqual(prediction_similarity(base, disjoint), Decimal("0.0000"))

    def test_nearest_neighbour_and_order_keep_identical_pair_adjacent(self) -> None:
        items = (
            prediction("a", (0, 1, 2), (Market.P1, Market.X, Market.P2)),
            prediction("b", (0, 1, 2), (Market.P1, Market.X, Market.P2)),
            prediction("c", (0, 1, 2), (Market.P2, Market.P1, Market.X)),
            prediction("d", (4, 5, 6), (Market.P1, Market.X, Market.P2)),
        )
        names = {item.participant_id: item.participant_id.upper() for item in items}
        neighbours = nearest_neighbours(items, names)
        self.assertEqual((neighbours["a"].participant_id, neighbours["a"].score), ("b", Decimal("1.0000")))
        ordered_ids = [item.participant_id for item in similarity_order(items, names)]
        self.assertEqual(abs(ordered_ids.index("a") - ordered_ids.index("b")), 1)

    def test_media_batches_rebalance_single_tail(self) -> None:
        self.assertEqual([len(item) for item in media_batches(tuple(range(20)))], [10, 10])
        self.assertEqual([len(item) for item in media_batches(tuple(range(21)))], [10, 9, 2])
        self.assertEqual([len(item) for item in media_batches(tuple(range(11)))], [9, 2])
        self.assertEqual([len(item) for item in media_batches((1,))], [1])

    def test_twenty_synthetic_players_render_two_batches_of_valid_png(self) -> None:
        predictions, results, participants = synthetic_data(ROUND, 20)
        cards = build_player_cards(ROUND, predictions, results, participants)
        self.assertEqual((len(cards), [len(item) for item in media_batches(cards)]), (20, [10, 10]))
        for card in cards:
            self.assertTrue(card.content.startswith(b"\x89PNG\r\n\x1a\n"))
            with Image.open(BytesIO(card.content)) as image:
                self.assertEqual(image.size, (CARD_WIDTH, CARD_HEIGHT))
                image.verify()

    def test_compact_bet_geometry_never_reaches_similarity_footer(self) -> None:
        predictions, _, _ = synthetic_data(ROUND, 1)
        boxes = card_bet_boxes(predictions[0])
        self.assertEqual(len(boxes), 5)
        self.assertTrue(all(first[1] < second[0] for first, second in zip(boxes, boxes[1:])))
        self.assertLessEqual(boxes[-1][1], FOOTER_TOP - 24)

    def test_demo_writes_cards_previews_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_demo(Path(directory), 20, ROOT / "data" / "fixtures_sample.csv")
            self.assertEqual(manifest["batch_sizes"], [10, 10])
            self.assertEqual(len(list(Path(directory).glob("player-card-*.png"))), 20)
            self.assertEqual(len(list(Path(directory).glob("batch-*-preview.png"))), 2)
            self.assertTrue((Path(directory) / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
