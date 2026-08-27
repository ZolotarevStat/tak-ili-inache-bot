from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tak_ili_inache import bot as bot_module
from tak_ili_inache.bot import BotService
from tak_ili_inache.delivery import UnknownDeliveryError
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import BetResult, Market

from test_bot_flow import FakeTelegram
from test_validators import valid_prediction


class InMemoryPublicationPngTests(unittest.TestCase):
    def setUp(self) -> None:
        self.round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")

    def _ready(self, output: str) -> tuple[FakeRepository, FakeTelegram, BotService]:
        repository, telegram = FakeRepository(), FakeTelegram()
        repository.save_round(self.round_)
        participant = repository.register_participant("42", "Игрок")
        base = valid_prediction()
        repository.save_prediction(base.__class__(self.round_.round_id, participant.participant_id, base.bets, base.submitted_at_msk), "p1")
        return repository, telegram, BotService(
            repository, telegram, lambda: self.round_.deadline_msk,
            admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
        )

    def _complete_results(self, repository: FakeRepository) -> None:
        for fixture in self.round_.fixtures:
            repository.save_result(BetResult(fixture.match_id, frozenset({Market.P1, Market.ONE_X, Market.TB})))

    @staticmethod
    def _outbox_callback(telegram: FakeTelegram, prefix: str) -> str:
        markup = telegram.messages[-1][2]
        for row in markup["inline_keyboard"]:
            for button in row:
                if button["callback_data"].startswith(prefix):
                    return button["callback_data"]
        raise AssertionError(f"outbox callback missing: {prefix}")

    @staticmethod
    def _callback(bot: BotService, data: str) -> None:
        bot._handle_callback({
            "id": "reconcile", "from": {"id": 99}, "data": data,
            "message": {"message_id": 1, "chat": {"id": 9, "type": "private"}},
        })

    def test_initial_interim_and_final_publish_pngs_from_bytes_without_disk_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            repository, telegram, bot = self._ready(output)
            bot._publish(9, "99")
            self.assertEqual(telegram.photo_bytes[-1][2][:8], b"\x89PNG\r\n\x1a\n")
            self.assertTrue(Path(telegram.documents[-1][1]).is_file())
            self.assertFalse(any(Path(output).rglob("*.png")))

            repository.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})))
            bot._publish_interim(9, "99")
            self.assertEqual(telegram.photo_bytes[-1][2][:8], b"\x89PNG\r\n\x1a\n")
            self.assertTrue(Path(telegram.documents[-1][1]).is_file())
            self.assertFalse(any(Path(output).rglob("*.png")))

            self._complete_results(repository)
            bot._score(9, "99")
            self.assertEqual(len([item for item in telegram.photo_bytes if item[0] == -100]), 4)
            self.assertTrue(all(Path(path).is_file() for _, path, _ in telegram.documents))
            self.assertFalse(any(Path(output).rglob("*.png")))

    def test_unknown_photo_reconciliation_never_needs_png_file_and_retries_only_when_undelivered(self) -> None:
        for delivered in (True, False):
            with self.subTest(delivered=delivered), tempfile.TemporaryDirectory() as output:
                repository, telegram, bot = self._ready(output)
                normal = telegram.send_photo_bytes
                telegram.send_photo_bytes = lambda *_args, **_kwargs: (_ for _ in ()).throw(UnknownDeliveryError("test"))
                with self.assertRaises(UnknownDeliveryError):
                    bot._publish(9, "99")
                self.assertFalse(any(Path(output).rglob("*.png")))
                self.assertEqual(len(repository.pending_operations()), 1)

                restarted = BotService(repository, telegram, lambda: self.round_.deadline_msk, admin_ids={"99"}, tournament_chat_id=-100, output_dir=output)
                restarted._outbox_menu(9, "99")
                prefix = "admin:outbox-done:" if delivered else "admin:outbox-retry:"
                self._callback(restarted, self._outbox_callback(telegram, prefix))
                if not delivered:
                    telegram.send_photo_bytes = normal
                before = len(telegram.photo_bytes)
                restarted._publish(9, "99")
                self.assertEqual(len(telegram.photo_bytes), before if delivered else before + 1)
                self.assertFalse(any(Path(output).rglob("*.png")))

    def test_render_failure_creates_no_png_and_later_retry_renders_from_durable_csv_source(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            _repository, telegram, bot = self._ready(output)
            original = bot_module.build_popularity_chart
            try:
                bot_module.build_popularity_chart = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failed"))
                bot._publish(9, "99")
            finally:
                bot_module.build_popularity_chart = original
            self.assertFalse(any(Path(output).rglob("*.png")))
            self.assertEqual(len(telegram.photo_bytes), 0)
            bot._publish(9, "99")
            self.assertEqual(len(telegram.photo_bytes), 1)
            self.assertFalse(any(Path(output).rglob("*.png")))
