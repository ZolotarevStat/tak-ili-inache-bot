from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.fixtures import import_fixtures
from test_validators import valid_prediction


class CsvRepositoryTests(unittest.TestCase):
    def test_close_intent_recovers_across_rounds_audit_crash_boundary_exactly_once(self) -> None:
        fixture_path = Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv"
        for interrupted_file, expected_status_before_restart in (("rounds.csv", "active"), ("audit_log.csv", "closed")):
            with self.subTest(interrupted_file=interrupted_file), tempfile.TemporaryDirectory() as directory:
                round_ = import_fixtures(fixture_path)
                repository = CsvRepository(directory)
                repository.save_round(round_)

                def interrupt(name, _temporary):
                    if name == interrupted_file:
                        raise RuntimeError("simulated close crash")

                repository._before_replace = interrupt
                with self.assertRaisesRegex(RuntimeError, "simulated close crash"):
                    repository.close_round(round_.round_id, actor_id="admin", closed_at="2026-08-25T12:00:00+03:00")
                self.assertEqual(repository.round_status(round_.round_id), expected_status_before_restart)

                recovered = CsvRepository(directory)
                self.assertEqual(recovered.round_status(round_.round_id), "closed")
                with (Path(directory) / "audit_log.csv").open(encoding="utf-8", newline="") as source:
                    closed_audits = [row for row in csv.DictReader(source) if row["action"] == "round_closed" and row["round_id"] == round_.round_id]
                self.assertEqual(len(closed_audits), 1)
                self.assertFalse(recovered.close_round(round_.round_id, actor_id="admin"))
                restarted_again = CsvRepository(directory)
                with (Path(directory) / "audit_log.csv").open(encoding="utf-8", newline="") as source:
                    self.assertEqual(sum(row["action"] == "round_closed" and row["round_id"] == round_.round_id for row in csv.DictReader(source)), 1)
                self.assertEqual(restarted_again.round_status(round_.round_id), "closed")

    def test_persists_participants_round_and_append_only_predictions_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory)
            round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
            repo.save_round(round_)
            participant = repo.register_participant("42", "Тестер")
            original = valid_prediction()
            prediction = original.__class__(original.round_id, participant.participant_id, original.bets, original.submitted_at_msk)
            repo.save_prediction(prediction)
            replacement = prediction.__class__(prediction.round_id, prediction.participant_id, prediction.bets, prediction.submitted_at_msk + timedelta(seconds=1))
            repo.save_prediction(replacement)

            restarted = CsvRepository(directory)
            self.assertEqual(restarted.get_participant("42"), participant)
            self.assertEqual(restarted.get_active_round(), round_)
            self.assertEqual(restarted.get_prediction("R1", participant.participant_id), replacement)
            self.assertEqual(restarted.raw_predictions(), (prediction, replacement))
            self.assertTrue((Path(directory) / "submissions_raw.csv").exists())
            self.assertTrue((Path(directory) / "submissions_latest.csv").exists())
