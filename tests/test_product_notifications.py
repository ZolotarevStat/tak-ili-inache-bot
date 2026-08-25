from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.bot import BotService
from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.delivery import UnknownDeliveryError
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import BetResult, Market, ProductNotification
from test_bot_flow import FakeTelegram
from test_validators import valid_prediction


ROOT = Path(__file__).resolve().parents[1]


class ProductNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.round_ = import_fixtures(ROOT / "data" / "fixtures_sample.csv")

    def test_round_opened_is_per_recipient_once_and_never_blocks_round_lifecycle(self) -> None:
        repo, telegram = FakeRepository(), FakeTelegram()
        repo.register_participant("42", "Первый")
        repo.register_participant("43", "Второй")
        bot = BotService(repo, telegram, lambda: self.round_.deadline_msk - timedelta(minutes=1), admin_ids={"99"})
        bot.pending_imports["99"] = self.round_

        bot._activate_pending(9, "99")

        opened = [message for message in telegram.messages if message[0] in {42, 43}]
        self.assertEqual(len(opened), 2)
        self.assertTrue(all("Открыт тур R1" in message[1] for message in opened))
        self.assertEqual({item.status for item in repo.product_notifications()}, {"done"})
        self.assertEqual(repo.pending_operations(), ())
        bot._recover_product_notifications()
        self.assertEqual(len([message for message in telegram.messages if message[0] in {42, 43}]), 2)
        self.assertTrue(repo.close_round("R1"))

    def test_outbox_planning_failure_does_not_block_activation(self) -> None:
        class FailingPlanRepository(FakeRepository):
            def ensure_product_notifications(self, notifications):
                raise OSError("test-only notification disk failure")

        repo, telegram = FailingPlanRepository(), FakeTelegram()
        repo.register_participant("42", "Первый")
        bot = BotService(repo, telegram, lambda: self.round_.deadline_msk - timedelta(minutes=1), admin_ids={"99"})
        bot.pending_imports["99"] = self.round_
        bot._activate_pending(9, "99")
        self.assertEqual(repo.get_active_round(), self.round_)
        self.assertEqual(repo.pending_operations(), ())
        self.assertEqual(repo.product_notifications(), ())

    def test_unknown_notification_delivery_is_persistent_and_never_blindly_replayed(self) -> None:
        class UnknownRecipientTelegram(FakeTelegram):
            def __init__(self) -> None:
                super().__init__()
                self.unknown_attempts = 0

            def send_message(self, chat_id, text, reply_markup=None):
                if chat_id == 42:
                    self.unknown_attempts += 1
                    raise UnknownDeliveryError("test-only unknown")
                return super().send_message(chat_id, text, reply_markup)

        repo, telegram = FakeRepository(), UnknownRecipientTelegram()
        repo.register_participant("42", "Первый")
        repo.register_participant("43", "Второй")
        bot = BotService(repo, telegram, lambda: self.round_.deadline_msk - timedelta(minutes=1), admin_ids={"99"})
        bot.pending_imports["99"] = self.round_
        bot._activate_pending(9, "99")

        self.assertEqual(telegram.unknown_attempts, 1)
        statuses = {item.recipient_fingerprint: item.status for item in repo.product_notifications()}
        self.assertIn("unknown", statuses.values())
        self.assertIn("done", statuses.values())
        self.assertEqual(repo.pending_operations(), ())
        bot._recover_product_notifications()
        self.assertEqual(telegram.unknown_attempts, 1)
        # Restart recovery keeps an ambiguous delivery unknown rather than
        # turning it into a new send attempt.
        BotService(repo, telegram, lambda: self.round_.deadline_msk - timedelta(minutes=1), admin_ids={"99"})
        self.assertEqual(telegram.unknown_attempts, 1)

    def test_post_send_persistence_failure_becomes_unknown_on_restart_without_replay(self) -> None:
        class FailingDoneRepository(FakeRepository):
            def __init__(self) -> None:
                super().__init__()
                self.fail_once = True

            def transition_product_notification(self, notification_key, expected_status, new_status):
                if self.fail_once and expected_status == "attempting" and new_status == "done":
                    self.fail_once = False
                    raise OSError("test-only persistence failure")
                return super().transition_product_notification(notification_key, expected_status, new_status)

        repo, telegram = FailingDoneRepository(), FakeTelegram()
        repo.register_participant("42", "Первый")
        bot = BotService(repo, telegram, lambda: self.round_.deadline_msk - timedelta(minutes=1), admin_ids={"99"})
        bot.pending_imports["99"] = self.round_
        bot._activate_pending(9, "99")
        self.assertEqual(len([item for item in telegram.messages if item[0] == 42]), 1)
        self.assertEqual(repo.product_notifications()[0].status, "attempting")

        BotService(repo, telegram, lambda: self.round_.deadline_msk - timedelta(minutes=1), admin_ids={"99"})
        self.assertEqual(repo.product_notifications()[0].status, "unknown")
        self.assertEqual(len([item for item in telegram.messages if item[0] == 42]), 1)

    def test_publish_and_scoring_notify_once_only_after_their_domain_completion(self) -> None:
        repo, telegram = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        first = repo.register_participant("42", "Первый")
        repo.register_participant("43", "Второй")
        base = valid_prediction()
        repo.save_prediction(base.__class__("R1", first.participant_id, base.bets, base.submitted_at_msk))
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(repo, telegram, lambda: self.round_.deadline_msk, admin_ids={"99"}, tournament_chat_id=-100, output_dir=output)
            bot._publish(9, "99")
            published = [item for item in telegram.messages if item[0] in {42, 43} and "Прогнозы тура R1 опубликованы" in item[1]]
            self.assertEqual(len(published), 2)
            bot._publish(9, "99")
            self.assertEqual(len([item for item in telegram.messages if item[0] in {42, 43} and "Прогнозы тура R1 опубликованы" in item[1]]), 2)

            for fixture in self.round_.fixtures:
                repo.save_result(BetResult(fixture.match_id, frozenset({Market.P1, Market.ONE_X, Market.TB})), "R1")
            bot._score(9, "99")
            ready = [item for item in telegram.messages if item[0] in {42, 43} and "Результаты тура R1 рассчитаны" in item[1]]
            self.assertEqual(len(ready), 2)
            self.assertTrue(repo.round_scored("R1"))
            bot._score(9, "99")
            self.assertEqual(len([item for item in telegram.messages if item[0] in {42, 43} and "Результаты тура R1 рассчитаны" in item[1]]), 2)
            self.assertEqual({item.status for item in repo.product_notifications()}, {"done"})

    def test_csv_outbox_is_additive_and_survives_restart(self) -> None:
        notification = ProductNotification("product:test:1", "R1", "round_opened", "checksum", "fingerprint")
        with tempfile.TemporaryDirectory() as directory:
            legacy = CsvRepository(directory)
            self.assertFalse((Path(directory) / "product_notifications.csv").exists())
            legacy.ensure_product_notifications((notification,))
            self.assertTrue(legacy.transition_product_notification(notification.notification_key, "planned", "ready"))
            restarted = CsvRepository(directory)
            self.assertEqual(
                restarted.product_notifications(),
                (ProductNotification("product:test:1", "R1", "round_opened", "checksum", "fingerprint", "ready"),),
            )
            self.assertFalse(restarted.transition_product_notification(notification.notification_key, "planned", "done"))
            self.assertTrue(restarted.transition_product_notification(notification.notification_key, "ready", "done"))
