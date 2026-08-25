from __future__ import annotations

import unittest
from pathlib import Path

from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from test_validators import valid_prediction


class FakeRepositoryTests(unittest.TestCase):
    def test_preserves_raw_history_and_replaces_latest_snapshot(self) -> None:
        repo = FakeRepository()
        round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
        repo.save_round(round_)
        first = valid_prediction()
        second = valid_prediction()
        repo.save_prediction(first)
        repo.save_prediction(second)
        self.assertIs(repo.get_round("R1"), round_)
        self.assertEqual(repo.get_prediction("R1", "p1"), second)
        self.assertEqual(repo.raw_predictions(), (first, second))
