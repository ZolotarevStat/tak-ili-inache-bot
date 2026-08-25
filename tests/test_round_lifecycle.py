from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.bot import BotService, Draft
from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import BetResult, Fixture, Market, Prediction, Round
from test_bot_flow import FakeTelegram
from test_validators import valid_prediction


ROOT = Path(__file__).resolve().parents[1]


def smoke_round(round_id: str = "SMOKE-20260824", kickoff_shift_days: int = -30) -> Round:
    source = import_fixtures(ROOT / "data" / "fixtures_sample.csv")
    fixtures = tuple(Fixture(round_id, item.match_id, item.kickoff_msk + timedelta(days=kickoff_shift_days), item.home_team, item.away_team, item.total_line, item.odds) for item in source.fixtures)
    return Round(round_id, fixtures, min(item.kickoff_msk for item in fixtures) - timedelta(minutes=1), f"checksum-{round_id}")


class SmokeRoundLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.tg = FakeRepository(), FakeTelegram()
        self.old = smoke_round()
        self.repo.save_round(self.old)
        self.now = [self.old.deadline_msk + timedelta(minutes=1)]
        self.bot = BotService(self.repo, self.tg, lambda: self.now[0], admin_ids={"99"}, tournament_chat_id=-100)
        self.player = self.repo.register_participant("42", "Игрок")
        base = valid_prediction()
        self.prediction = Prediction(self.old.round_id, self.player.participant_id, base.bets, base.submitted_at_msk)
        self.repo.save_prediction(self.prediction)
        # Published state deliberately has a partial result: smoke close must
        # archive it without deleting it or requiring a scoring run.
        self.bot._publish(9, "99")
        self.repo.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})), self.old.round_id)

    def test_published_partial_smoke_round_closes_then_next_round_activates(self) -> None:
        self.bot.handle_update(self._admin_callback("admin:close"))
        self.assertIn("без расчёта", self.tg.messages[-1][1])
        close_confirmation = self._close_confirmation()
        self.bot.handle_update(self._admin_callback(close_confirmation))
        self.assertIsNone(self.repo.get_active_round())
        self.assertEqual(self.repo.round_status(self.old.round_id), "closed")
        self.assertEqual(self.repo.get_prediction(self.old.round_id, self.player.participant_id), self.prediction)
        self.assertEqual(self.repo.results(self.old.round_id)[0].match_id, "M01")
        # Repeat/stale confirmation is idempotent and cannot reopen history.
        self.bot.handle_update(self._admin_callback(close_confirmation))
        self.assertIsNone(self.repo.get_active_round())

        content = (ROOT / "data" / "fixtures_smoke_20260907.csv").read_bytes()
        self.bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "document": {"file_name": "fixtures_smoke_20260907.csv", "content": content}}})
        self.assertIn("Активировать новый тур", [row[0]["text"] for row in self.tg.messages[-1][2]["inline_keyboard"]])
        self.bot.handle_update(self._admin_callback("admin:activate"))
        self.assertEqual(self.repo.get_active_round().round_id, "SMOKE-20260907")
        self.now[0] = self.repo.get_active_round().deadline_msk - timedelta(minutes=5)
        self.bot.handle_update({"message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "И"}, "text": "/predict"}})
        self.assertEqual(self.bot._draft("42").round_id, "SMOKE-20260907")

    def test_new_round_upload_is_blocked_until_active_round_is_terminal(self) -> None:
        content = (ROOT / "data" / "fixtures_smoke_20260907.csv").read_bytes()
        self.bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "document": {"file_name": "fixtures_smoke_20260907.csv", "content": content}}})
        self.assertIn("сначала завершите активный тур", self.tg.messages[-1][1])
        self.assertNotIn("admin:activate", str(self.tg.messages[-1][2]))

    def test_same_round_replace_remains_fail_closed_and_pending_blocks_close(self) -> None:
        self.bot.pending_imports["99"] = self.old
        self.bot._replace_pending(9, "99")
        self.assertIn("Перезапись заблокирована", self.tg.messages[-1][1])
        self.repo.begin_operation("publish:SMOKE-20260824:pending")
        self.bot._request_close(9, "99")
        self.assertIn("незавершённые отправки", self.tg.messages[-1][1])

    def test_future_smoke_is_visible_in_admin_and_closes_without_deadline(self) -> None:
        future = smoke_round("SMOKE-FUTURE", kickoff_shift_days=30)
        repo, tg = FakeRepository(), FakeTelegram(); repo.save_round(future)
        bot = BotService(repo, tg, lambda: future.deadline_msk - timedelta(hours=1), admin_ids={"99"})
        bot._admin_menu(9, "99")
        self.assertIn("Закрыть тестовый тур без расчёта", [row[0]["text"] for row in tg.messages[-1][2]["inline_keyboard"]])
        bot.handle_update(self._admin_callback("admin:close"))
        confirmation = next(row[0]["callback_data"] for row in tg.messages[-1][2]["inline_keyboard"] if row[0]["text"] == "Подтвердить завершение")
        bot.handle_update(self._admin_callback(confirmation))
        self.assertEqual(repo.round_status(future.round_id), "closed")
        self.assertIsNone(repo.get_active_round())

    def test_stale_close_confirmation_cannot_close_next_round(self) -> None:
        self.bot.handle_update(self._admin_callback("admin:close"))
        stale = self._close_confirmation()
        self.bot.handle_update(self._admin_callback(stale))
        next_round = smoke_round("SMOKE-NEXT", kickoff_shift_days=30)
        self.repo.save_round(next_round)
        self.bot.handle_update(self._admin_callback(stale))
        self.assertEqual((self.repo.round_status(self.old.round_id), self.repo.round_status(next_round.round_id)), ("closed", "active"))
        self.assertIn("устарело", self.tg.messages[-1][1])

    def test_close_confirmation_requires_exact_token_and_requesting_admin(self) -> None:
        self.bot.admin_ids.add("98")
        self.bot.handle_update(self._admin_callback("admin:close"))
        confirmation = self._close_confirmation()
        for payload, actor in (
            ("admin:close-confirm:", "99"),
            ("admin:close-confirm:wrong", "99"),
            (confirmation, "98"),
        ):
            self.bot.handle_update(self._admin_callback(payload, actor))
            self.assertEqual(self.repo.round_status(self.old.round_id), "active")
        self.bot.handle_update(self._admin_callback(confirmation))
        self.assertEqual(self.repo.round_status(self.old.round_id), "closed")
        # The one-shot callback remains a no-mutation stale control.
        self.bot.handle_update(self._admin_callback(confirmation))
        self.assertIsNone(self.repo.get_active_round())

    def test_old_draft_is_read_only_after_archive_and_cannot_mix_into_new_round(self) -> None:
        self.bot.drafts["42"] = Draft(self.old.round_id, draft_id="old", chat_id=42, active_message_id=7)
        self.bot.handle_update(self._admin_callback("admin:close"))
        self.bot.handle_update(self._admin_callback(self._close_confirmation()))
        content = (ROOT / "data" / "fixtures_smoke_20260907.csv").read_bytes()
        self.bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "document": {"file_name": "fixtures_smoke_20260907.csv", "content": content}}})
        self.bot._activate_pending(9, "99")
        self.now[0] = self.repo.get_active_round().deadline_msk - timedelta(minutes=5)
        self.bot.handle_update({"callback_query": {"id": "stale", "from": {"id": 42}, "data": "d:old:1:resume", "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}}}})
        self.assertIsNone(self.bot._draft("42"))
        self.bot.handle_update({"message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "И"}, "text": "/predict"}})
        self.assertEqual(self.bot._draft("42").round_id, "SMOKE-20260907")

    def test_regular_round_needs_scoring_before_terminal_close(self) -> None:
        regular = smoke_round("R-CLOSED", kickoff_shift_days=30)
        repo, tg = FakeRepository(), FakeTelegram(); repo.save_round(regular)
        now = [regular.deadline_msk - timedelta(minutes=1)]
        bot = BotService(repo, tg, lambda: now[0], admin_ids={"99"})
        bot._admin_menu(9, "99")
        self.assertNotIn("admin:close", str(tg.messages[-1][2]))
        bot._request_close(9, "99")
        self.assertIn("только после дедлайна", tg.messages[-1][1])
        now[0] = regular.deadline_msk
        bot._request_close(9, "99")
        self.assertIn("только после успешного скоринга", tg.messages[-1][1])
        repo.mark_round_scored(regular.round_id)
        bot.handle_update(self._admin_callback("admin:close"))
        confirmation = next(row[0]["callback_data"] for row in tg.messages[-1][2]["inline_keyboard"] if row[0]["text"] == "Подтвердить завершение")
        bot.handle_update(self._admin_callback(confirmation))
        self.assertIsNone(repo.get_active_round())

    @staticmethod
    def _admin_callback(data: str, actor_id: str = "99") -> dict:
        return {"callback_query": {"id": data, "from": {"id": int(actor_id)}, "data": data, "message": {"message_id": 1, "chat": {"id": 9, "type": "private"}}}}

    def _close_confirmation(self) -> str:
        return next(row[0]["callback_data"] for row in self.tg.messages[-1][2]["inline_keyboard"] if row[0]["text"] == "Подтвердить завершение")


class MultiRoundCsvRepositoryTests(unittest.TestCase):
    def test_history_results_scopes_and_legacy_result_migration_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory)
            old, new = smoke_round(), import_fixtures(ROOT / "data" / "fixtures_smoke_20260907.csv")
            repo.save_round(old)
            repo.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})), old.round_id)
            # Mimic the pre-migration single-round on-disk results layout.
            path = Path(directory) / "match_results.csv"
            with path.open(encoding="utf-8", newline="") as source:
                legacy = [{key: value for key, value in row.items() if key != "round_id"} for row in csv.DictReader(source)]
            with path.open("w", encoding="utf-8", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=["match_id", "winning_markets", "returned_markets"])
                writer.writeheader(); writer.writerows(legacy)
            restarted = CsvRepository(directory)
            self.assertEqual(restarted.results(old.round_id)[0].match_id, "M01")
            self.assertEqual(restarted.raw_result_rows()[0]["round_id"], old.round_id)
            restarted.close_round(old.round_id)
            restarted.save_round(new)
            # Same match key would still be isolated by round_id if future
            # lines happen to reuse it; current acceptance file uses unique IDs.
            restarted.save_result(BetResult("S0907-01", frozenset({Market.P2, Market.X_TWO, Market.TM})), new.round_id)
            self.assertEqual(restarted.get_round(old.round_id), old)
            self.assertEqual(restarted.get_active_round().round_id, new.round_id)
            self.assertEqual(restarted.results(old.round_id)[0].winning_markets, frozenset({Market.P1, Market.ONE_X, Market.TB}))
            self.assertEqual(restarted.results(new.round_id)[0].match_id, "S0907-01")
            with self.assertRaises(ValueError):
                restarted.save_round(smoke_round("SMOKE-THIRD"))
            with self.assertRaises(ValueError):
                restarted.replace_round(smoke_round("SMOKE-THIRD"))
