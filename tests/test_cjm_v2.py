from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.bot import BotService
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures

from tests.test_cjm_v1 import Telegram


class CjmV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.tg = FakeRepository(), Telegram()
        self.round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
        self.repo.save_round(self.round_)
        self.now = [self.round_.deadline_msk - timedelta(minutes=10)]
        self.bot = BotService(self.repo, self.tg, lambda: self.now[0])
        self.message("/start")

    def test_five_slots_show_live_odds_reject_duplicates_and_confirm(self) -> None:
        self.message("/predict")
        self.assertIn("пять ставок", self.card()[2])
        for index, match_id in enumerate(("M01", "M02", "M03", "M04"), 1):
            self.click(f"{index}. Добавить ставку")
            self.click("Ординар · одно событие")
            self.click_data(lambda data, match_id=match_id: data.endswith(f"slot:match:{match_id}"))
            self.click_data(lambda data, match_id=match_id: data.endswith(f"slot:market:{match_id}:П1"))
            self.click("✅ Сохранить ставку")
        self.click("5. Добавить ставку")
        self.click("Экспресс · 2–3 события")
        for match_id in ("M05", "M06"):
            self.click_data(lambda data, match_id=match_id: data.endswith(f"slot:match:{match_id}"))
            self.click_data(lambda data, match_id=match_id: data.endswith(f"slot:market:{match_id}:П1"))
        self.click("✅ Сохранить ставку")
        self.assertIn("кэф", self.card()[2])
        self.assertIn("💰 Распределить банк", self.labels())

        # While editing a slot, an event already used elsewhere has no active
        # selection callback and cannot get into the saved coupon twice.
        self.click_data(lambda data: data.endswith("slot:open:1"))
        self.click("Ординар · одно событие")
        blocked = next(button for row in self.card()[3]["inline_keyboard"] for button in row if button["callback_data"].endswith("slot:occupied"))
        self.callback(blocked["callback_data"], "occupied")
        self.assertIn("уже используется", self.tg.messages[-1][2])
        self.click("← Назад")

        self.click("💰 Распределить банк")
        for stake in (1000, 1000, 1000, 500, 1500):
            self.callback(f"stake:{stake}", "stake")
        self.assertIn("Проверьте прогноз", self.card()[2])
        self.click("✅ Подтвердить")
        prediction = self.repo.get_prediction(self.round_.round_id, self.repo.get_participant("42").participant_id)
        self.assertEqual((len(prediction.bets), sum(bet.stake for bet in prediction.bets)), (5, 5000))
        self.assertEqual((sum(bet.bet_type.value == "single" for bet in prediction.bets), sum(bet.bet_type.value == "express" for bet in prediction.bets)), (4, 1))

    def card(self):
        draft = self.bot._draft("42")
        if draft and draft.active_message_id is not None:
            for collection in (self.tg.edits, self.tg.messages):
                for item in reversed(collection):
                    if item[1] == draft.active_message_id:
                        return item
        return self.tg.current

    def labels(self):
        return [button["text"] for row in self.card()[3]["inline_keyboard"] for button in row]

    def click(self, label: str):
        for row in self.card()[3]["inline_keyboard"]:
            for button in row:
                if button["text"] == label:
                    self.callback(button["callback_data"], label)
                    return
        self.fail(f"button {label!r} unavailable: {self.labels()}")

    def click_data(self, predicate):
        for row in self.card()[3]["inline_keyboard"]:
            for button in row:
                if predicate(button["callback_data"]):
                    self.callback(button["callback_data"], "data")
                    return
        self.fail("callback unavailable")

    def callback(self, data: str, callback_id: str) -> None:
        draft = self.bot._draft("42")
        self.bot.handle_update({"update_id": len(self.tg.answers) + 1, "callback_query": {"id": callback_id, "from": {"id": 42}, "data": data, "message": {"message_id": draft.active_message_id, "chat": {"id": 1, "type": "private"}}}})

    def message(self, text: str) -> None:
        self.bot.handle_update({"message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 42, "first_name": "Т"}, "text": text}})
