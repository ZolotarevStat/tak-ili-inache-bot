from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Market
from tak_ili_inache.validators import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "fixtures_sample.csv"


class FixtureImportTests(unittest.TestCase):
    def test_imports_sample_and_calculates_server_deadline(self) -> None:
        round_ = import_fixtures(SAMPLE)
        self.assertEqual(round_.round_id, "R1")
        self.assertEqual(len(round_.fixtures), 12)
        self.assertEqual(round_.deadline_msk.isoformat(), "2026-06-11T18:59:00+03:00")
        self.assertEqual(str(round_.fixtures[0].odds[Market.P1]), "1.80")
        self.assertEqual(str(round_.fixtures[0].total_line), "2.5")
        self.assertEqual(len(round_.checksum), 64)

    def test_rejects_missing_required_column(self) -> None:
        self.assert_fixture_error(_mutate(lambda rows: _drop(rows, "total_line")), "обязательные")

    def test_rejects_not_enough_matches(self) -> None:
        self.assert_fixture_error(_mutate(lambda rows: rows[:10]), "от 11 до 14")

    def test_rejects_duplicate_match_and_bad_odds(self) -> None:
        def duplicate(rows):
            rows[-1]["match_id"] = rows[0]["match_id"]
            return rows
        self.assert_fixture_error(_mutate(duplicate), "не должен повторяться")
        self.assert_fixture_error(_mutate(lambda rows: _set(rows, "odds_p1", "1.00")), "больше 1")

    def test_rejects_mixed_round_and_bad_datetime(self) -> None:
        self.assert_fixture_error(_mutate(lambda rows: _set_one(rows, 1, "round_id", "R2")), "одному round_id")
        self.assert_fixture_error(_mutate(lambda rows: _set_one(rows, 0, "kickoff_msk", "tomorrow")), "ISO datetime")

    def test_keeps_seconds_in_exact_one_minute_deadline_and_rejects_non_finite_decimal(self) -> None:
        round_ = import_fixtures(_write_fixture(_mutate(lambda rows: _set_one(rows, 0, "kickoff_msk", "2026-06-11T19:00:30+03:00"))))
        self.assertEqual(round_.deadline_msk.isoformat(), "2026-06-11T18:59:30+03:00")
        self.assert_fixture_error(_mutate(lambda rows: _set_one(rows, 0, "odds_p1", "Infinity")), "десятичным")
        self.assert_fixture_error(_mutate(lambda rows: _set_one(rows, 0, "odds_p1", "NaN")), "десятичным")

    def assert_fixture_error(self, content: str, expected: str) -> None:
        path = _write_fixture(content)
        self.addCleanup(path.unlink)
        with self.assertRaisesRegex(ValidationError, expected):
            import_fixtures(path)


def _mutate(change):
    with SAMPLE.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    changed = change(rows)
    columns = list(rows[0])
    if changed and set(changed[0]) != set(columns):
        columns.remove("total_line")
    import io
    target = io.StringIO()
    writer = csv.DictWriter(target, fieldnames=columns)
    writer.writeheader()
    writer.writerows(changed)
    return target.getvalue()


def _set(rows, field, value):
    for row in rows:
        row[field] = value
    return rows


def _set_one(rows, index, field, value):
    rows[index][field] = value
    return rows


def _drop(rows, field):
    for row in rows:
        row.pop(field)
    return rows


def _write_fixture(content):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".csv", delete=False) as file:
        file.write(content)
        return Path(file.name)
