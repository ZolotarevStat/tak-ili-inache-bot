from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.bot import BotService
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.late_predictions import import_late_predictions
from tak_ili_inache.validators import ValidationError
from test_bot_flow import FakeTelegram


ROOT = Path(__file__).resolve().parents[1]


class LatePredictionsAndRemindersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.round_ = import_fixtures(ROOT / "data" / "fixtures_sample.csv")
        self.repo = FakeRepository(); self.repo.save_round(self.round_)
        self.player = self.repo.register_participant("42", "Игрок")

    def test_late_csv_uses_registered_name_and_accepts_admin_approved_started_fixture(self) -> None:
        now = self.round_.deadline_msk + timedelta(minutes=1)
        content = (
            "round_id,display_name,bet_no,bet_type,stake,match_id,market\n"
            "R1,Игрок,1,single,1000,M02,П1\nR1,Игрок,2,single,1000,M03,Х\n"
            "R1,Игрок,3,single,1000,M04,П2\nR1,Игрок,4,single,500,M05,ТБ\n"
            "R1,Игрок,5,express,1500,M06,П1\nR1,Игрок,5,express,1500,M07,ТМ\n"
        ).encode()
        imported = import_late_predictions(content, self.round_, self.repo.participants(), now)
        self.assertEqual((len(imported), imported[0].participant_id, len(imported[0].bets)), (1, self.player.participant_id, 5))
        started = content.replace(b"M02", b"M01", 1)
        approved = import_late_predictions(started, self.round_, self.repo.participants(), now)
        self.assertEqual(approved[0].bets[0].events[0].match_id, "M01")

    def test_reminders_are_sent_once_only_to_missing_people(self) -> None:
        other = self.repo.register_participant("43", "Не сдал")
        now = [self.round_.deadline_msk.replace(hour=12, minute=0, second=1, microsecond=0)]
        telegram = FakeTelegram(); bot = BotService(self.repo, telegram, lambda: now[0])
        bot.process_scheduled_notifications(); bot.process_scheduled_notifications()
        self.assertEqual([chat_id for chat_id, *_ in telegram.messages], [int(self.player.telegram_id), int(other.telegram_id)])
        now[0] = self.round_.deadline_msk - timedelta(minutes=59)
        bot.process_scheduled_notifications()
        self.assertEqual(len(telegram.messages), 4)
