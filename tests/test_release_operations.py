from __future__ import annotations

import csv
import multiprocessing
import os
import re
import tempfile
import unittest
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.operations import create_backup, health, restore_backup
from tak_ili_inache.liveness import LivenessStore
from tak_ili_inache.models import BetResult, Participant, Market
from tak_ili_inache.reporting import build_reports, chart_series
from PIL import Image
from test_validators import valid_prediction

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_digest import included_files


def _save_in_process(directory: str, prediction, update_id: str) -> None:
    repository = CsvRepository(directory)

    def kill_before_latest(name, _path):
        if name == "submissions_latest.csv":
            os.kill(os.getpid(), 9)
    repository._before_replace = kill_before_latest
    repository.save_prediction(prediction, update_id)


class ReleaseOperationsTests(unittest.TestCase):
    def test_round_commit_marker_is_written_after_fixtures_and_health_detects_incomplete_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
            repo = CsvRepository(directory)
            def kill_before_round(name, _path):
                if name == "rounds.csv":
                    raise RuntimeError("simulated interruption")
            repo._before_replace = kill_before_round
            with self.assertRaises(RuntimeError):
                repo.save_round(round_)
            self.assertIsNone(CsvRepository(directory).get_active_round())
            # A manually corrupted committed marker is not healthy.
            (Path(directory) / "rounds.csv").write_text("round_id,deadline_msk,checksum\nR1,2026-06-11T18:59:00+03:00,x\n", encoding="utf-8")
            (Path(directory) / "fixtures.csv").unlink()
            self.assertFalse(health(directory)["ok"])

    def test_health_rejects_wrong_fixture_count_and_backup_is_restorable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory)
            round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
            repo.save_round(round_)
            fixtures_path = Path(directory) / "fixtures.csv"
            rows = fixtures_path.read_text(encoding="utf-8").splitlines()
            fixtures_path.write_text("\n".join(rows[:2]) + "\n", encoding="utf-8")
            self.assertFalse(health(directory)["ok"])

    def test_activation_audit_has_line_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
            CsvRepository(directory).save_round(round_, actor_id="admin-test", imported_at="2026-01-01T00:00:00+03:00")
            with (Path(directory) / "audit_log.csv").open(encoding="utf-8") as source:
                row = next(csv.DictReader(source))
            self.assertEqual(row["line_version"], round_.checksum[:12])

    def test_health_and_backup_reject_alien_duplicate_incompatible_and_malformed_result_rows(self) -> None:
        cases = [
            ["ALIEN", '["П1"]', '[]'],
            ["M01", '["П1"]', '[]'],
            ["M01", '["П1","1Х","ТБ"]', '[]'],
            ["M01", "not-json", "[]"],
        ]
        for case_index, bad_row in enumerate(cases):
            with self.subTest(bad_row=bad_row), tempfile.TemporaryDirectory() as directory:
                repo = CsvRepository(directory)
                round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
                repo.save_round(round_)
                rows = [[fixture.match_id, '["П1","1Х","ТБ"]', '[]'] for fixture in round_.fixtures]
                rows[0] = bad_row
                # duplicate case needs a separately valid first row plus duplicate M01.
                if case_index == 1:
                    rows.append(["M01", '["П1","1Х","ТБ"]', '[]'])
                    rows[0] = ["M01", '["П1","1Х","ТБ"]', '[]']
                elif case_index == 2:
                    rows.append(["M01", '["П1","1Х","ТБ"]', '[]'])
                with (Path(directory) / "match_results.csv").open("w", encoding="utf-8", newline="") as target:
                    writer = csv.writer(target)
                    writer.writerow(["match_id", "winning_markets", "returned_markets"])
                    writer.writerows(rows)
                self.assertFalse(health(directory)["ok"])
                with self.assertRaises(ValueError):
                    create_backup(directory, Path(directory).parent / "invalid-results.tar.gz")

    def test_health_accepts_valid_partial_result_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory)
            round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
            repo.save_round(round_)
            repo.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})))
            self.assertTrue(health(directory)["ok"])

    def test_health_reports_stale_worker_even_when_csv_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = [__import__("datetime").datetime(2026, 8, 24, 9, 0, tzinfo=__import__("datetime").timezone.utc)]
            liveness = LivenessStore(directory, now=lambda: now[0])
            liveness.start()
            liveness.successful_poll()
            now[0] += timedelta(seconds=91)
            report = health(directory, require_liveness=True, now=lambda: now[0])
            self.assertTrue(report["data_ok"])
            self.assertFalse(report["ok"])
            self.assertFalse(report["liveness"]["ok"])
    def test_concurrent_prediction_saves_keep_valid_raw_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = valid_prediction()
            predictions = [base.__class__(base.round_id, f"p{i}", base.bets, base.submitted_at_msk + timedelta(seconds=i)) for i in range(8)]
            threads = []
            import threading
            for i, prediction in enumerate(predictions):
                thread = threading.Thread(target=CsvRepository(directory).save_prediction, args=(prediction, f"u{i}"))
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            restored = CsvRepository(directory)
            self.assertEqual(len(restored.raw_predictions()), 8)
            self.assertEqual(len(restored.latest_predictions("R1")), 8)
            with (Path(directory) / "submissions_latest.csv").open(encoding="utf-8") as source:
                self.assertEqual(len(list(csv.DictReader(source))), 8)

    def test_kill_during_latest_write_leaves_latest_parseable_and_replay_repairs_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = valid_prediction()
            first = base.__class__(base.round_id, "p1", base.bets, base.submitted_at_msk)
            second = base.__class__(base.round_id, "p1", base.bets, base.submitted_at_msk + timedelta(seconds=1))
            repository = CsvRepository(directory)
            repository.save_prediction(first, "u1")
            process = multiprocessing.Process(target=_save_in_process, args=(directory, second, "u2"))
            process.start()
            process.join(10)
            self.assertNotEqual(process.exitcode, 0)
            restarted = CsvRepository(directory)
            # Repository-open recovery rebuilds latest from durable raw without a Telegram replay.
            self.assertEqual(restarted.get_prediction("R1", "p1"), second)
            self.assertEqual(len(restarted.raw_predictions()), 2)
            self.assertFalse(restarted.save_prediction(second, "u2"))
            self.assertEqual(restarted.get_prediction("R1", "p1"), second)
            self.assertEqual(len(restarted.raw_predictions()), 2)

    def test_backup_restore_health_and_idempotent_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as restore_dir:
            repo = CsvRepository(directory)
            round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
            repo.save_round(round_)
            prediction = valid_prediction()
            self.assertTrue(repo.save_prediction(prediction, "same-update"))
            self.assertFalse(repo.save_prediction(prediction, "same-update"))
            output = Path(directory) / "output"
            output.mkdir()
            (output / "leaderboard.csv").write_text("rank,payout\n1,5000\n", encoding="utf-8")
            (output / "leaderboard.png").write_bytes(b"derived chart")
            archive = Path(directory).parent / "tak-ili-inache-test-backup.tar.gz"
            create_backup(directory, archive)
            restore_backup(archive, restore_dir)
            restored = CsvRepository(restore_dir)
            self.assertEqual(restored.get_active_round().deadline_msk.tzinfo.key, "Europe/Moscow")
            self.assertEqual(len(restored.raw_predictions()), 1)
            self.assertTrue((Path(restore_dir) / "output" / "leaderboard.csv").is_file())
            self.assertFalse((Path(restore_dir) / "output" / "leaderboard.png").exists())
            self.assertTrue(health(restore_dir)["ok"])
            archive.unlink()

    def test_repeat_scoring_replaces_reports_without_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            prediction = valid_prediction()
            results = tuple(BetResult(f"M{i:02d}", frozenset({Market.P1})) for i in range(1, 13))
            participant = Participant("p1", "test-id", "Тестер")
            first = build_reports(output, "R1", (prediction,), results, (participant,), prediction.submitted_at_msk)
            before = first["scoring"].read_text(encoding="utf-8")
            second = build_reports(output, "R1", (prediction,), results, (participant,), prediction.submitted_at_msk + timedelta(hours=1))
            after = second["scoring"].read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertEqual(first["scoring"].parent, second["scoring"].parent)
            with second["scoring"].open(encoding="utf-8") as source:
                self.assertEqual(len(list(csv.DictReader(source))), 5)

    def test_png_charts_are_valid_and_match_the_csv_scoring_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            prediction = valid_prediction()
            results = tuple(BetResult(f"M{i:02d}", frozenset({Market.P1})) for i in range(1, 13))
            participant = Participant("p1", "test-id", "Тестер")
            paths = build_reports(output, "R1", (prediction,), results, (participant,), prediction.submitted_at_msk)
            with paths["scoring"].open(encoding="utf-8") as source:
                scoring = list(csv.DictReader(source))
            with paths["leaderboard"].open(encoding="utf-8") as source:
                leaderboard = list(csv.DictReader(source))
            series = chart_series(leaderboard, scoring)
            self.assertEqual(series["chart_leaderboard"], [("Тестер", int(leaderboard[0]["gross_payout"]))])
            self.assertEqual(
                dict(series["chart_bet_types"]),
                {
                    "Ординары": sum(int(row["gross_payout"]) for row in scoring if row["bet_type"] == "single"),
                    "Экспрессы": sum(int(row["gross_payout"]) for row in scoring if row["bet_type"] == "express"),
                },
            )
            popularity, bank_by_event = Counter(), Counter()
            for row in scoring:
                for event in row["event_details"].split("; "):
                    popularity[event] += 1
                    bank_by_event[event] += int(row["stake"])
            self.assertEqual(
                series["chart_popularity"],
                [(f"{event} ({bank_by_event[event]})", count) for event, count in popularity.most_common(12)],
            )
            for key in ("chart_leaderboard", "chart_bet_types", "chart_popularity"):
                chart = paths[key]
                self.assertEqual(chart.suffix, ".png")
                self.assertEqual(chart.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                with Image.open(chart) as image:
                    image.verify()
                with Image.open(chart) as image:
                    self.assertGreater(image.width, 0)
                    self.assertGreater(image.height, 0)

    def test_deployment_and_smoke_instructions_match_the_release_artifacts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        runbook = (root / "RUNBOOK.md").read_text(encoding="utf-8")
        smoke = (root / "LOCAL_SMOKE.md").read_text(encoding="utf-8")
        manifest = (root / "RELEASE_MANIFEST.md").read_text(encoding="utf-8")
        manifest_id = re.search(r"^\| Release ID \| `([^`]+)` \|$", manifest, re.MULTILINE)
        self.assertIsNotNone(manifest_id)
        candidate_id = manifest_id.group(1)
        exported_ids = re.findall(r'^export TII_RELEASE_ID="([^"]+)"$', runbook, re.MULTILINE)
        self.assertEqual(len(exported_ids), 2)
        self.assertTrue(all(item == candidate_id for item in exported_ids))
        manifest_digest = re.search(r"^\| Candidate runtime digest ×2 \| `(sha256:[0-9a-f]{64})` \|$", manifest, re.MULTILINE)
        self.assertIsNotNone(manifest_digest)
        runtime_digest = manifest_digest.group(1)
        test_count = 158
        self.assertNotIn("--no-build-isolation", runbook)
        for artifact in (manifest, smoke, runbook):
            self.assertIn(candidate_id, artifact)
            self.assertIn(runtime_digest, artifact)
            self.assertIn(f"Ran {test_count} tests", artifact)
        self.assertIn("Sequential-round acceptance", smoke)
        self.assertRegex(runbook, r"Any active\s+`SMOKE-\*` round, before or after its deadline")
        self.assertNotIn("An expired\n`SMOKE-*` round", runbook)
        self.assertIn("три PNG-графика", smoke)
        self.assertNotIn("три SVG-графика", smoke)
        self.assertIn("tak-ili-inache-upgrade-wrapper", runbook)
        self.assertIn("tak-ili-inache-admin activate", runbook)

    def test_release_digest_excludes_planning_documents_and_test_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        included = {path.relative_to(root).as_posix() for path in included_files(root)}
        self.assertIn("src/tak_ili_inache/polling.py", included)
        self.assertIn("deploy/tak-ili-inache.service", included)
        self.assertNotIn("research_plan.md", included)
        self.assertNotIn("task_context.md", included)
        self.assertNotIn("RELEASE_MANIFEST.md", included)
        self.assertNotIn("tests/test_polling.py", included)
