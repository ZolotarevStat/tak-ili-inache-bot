from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.bot import BotService
from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.draft_store import DraftStore
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures

from tests.test_cjm_v1 import Telegram


class CjmV12Tests(unittest.TestCase):
    """AC-46…AC-58: partial removal and typed confirmed correction."""

    def setUp(self):
        self.repo, self.tg = FakeRepository(), Telegram()
        self.round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
        self.repo.save_round(self.round_)
        self.now = [self.round_.deadline_msk - timedelta(minutes=10)]
        self.bot = BotService(self.repo, self.tg, lambda: self.now[0], composer_version=1)
        self._card_override = None
        self.message("/start")

    def test_simple_removal_preserves_draft_page_and_blocks_finish_at_five(self):  # AC-46, AC-47, AC-49, AC-51
        self.message("/predict")
        for number in range(1, 7):
            self.pick(f"M{number:02}")
        draft = self.bot._draft("42")
        before_revision, before_page = draft.revision, draft.match_page
        self.open_match("M01")
        remove = self.button("🗑 Убрать матч")
        self.callback(remove, draft.active_message_id, "remove-first")
        self.assertEqual((len(draft.current_events), draft.match_page), (5, before_page))
        self.assertNotIn("M01", draft.selection_slots)
        self.assertEqual(self.label("✅ 6+ событий"), "✅ 6+ событий")
        revision = draft.revision
        self.callback(remove, draft.active_message_id, "remove-stale")
        self.assertEqual((len(draft.current_events), draft.revision), (5, revision))
        self.now[0] = self.round_.deadline_msk
        self.message("/predict")
        self.assertEqual(draft.phase, "L0")
        self.assertNotIn("🗑 Убрать матч", self.labels())
        self.assertGreater(draft.revision, before_revision)

    def test_removal_never_turns_a_draft_into_an_empty_snapshot(self):
        self.message("/predict"); self.pick("M01"); self.open_match("M01")
        draft = self.bot._draft("42"); before = (tuple(draft.current_events), draft.revision)
        self.click("🗑 Убрать матч")
        self.assertEqual((tuple(draft.current_events), draft.revision), before)
        self.assertIn("хотя бы одно событие", self.tg.messages[-1][2])

    def test_downstream_removal_warns_then_reconciles_atomically(self):  # AC-48, AC-49, AC-50
        self._confirmed_coupon(7)
        confirmed = self.prediction()
        self.message("/my")
        self._card_override = self.tg.messages[-1]
        self.click("✏️ Внести корректировки")
        draft = self.bot._draft("42")
        self.assertEqual(draft.replacement_kind, "correction")
        self.click("⚽ Изменить события")
        self.open_match("M01")
        self.click("🗑 Убрать матч")
        self.assertEqual(draft.phase, "W1")
        before = (tuple(draft.current_events), tuple((bet.bet_id, bet.stake) for bet in draft.bets))
        self.click("← Оставить без изменений")
        self.assertEqual((tuple(draft.current_events), tuple((bet.bet_id, bet.stake) for bet in draft.bets)), before)
        self.click("← Назад")
        self.open_match("M01")
        self.click("🗑 Убрать матч")
        self.click("✅ Убрать матч")
        self.assertEqual((len(draft.current_events), draft.phase), (6, "P1"))
        self.assertEqual((len(draft.bets), sum(bet.stake or 0 for bet in draft.bets)), (5, 5000))
        self.assertNotIn("M01", {event.match_id for event in draft.current_events})
        self.assertEqual(self.prediction(), confirmed)  # correction has not replaced confirmed
        stale = self.button("✅ Подтвердить")
        self.click("✅ Подтвердить")
        self.assertNotEqual(self.prediction(), confirmed)
        # A pre-confirm card cannot replay the deletion or overwrite the new prediction.
        self.callback(stale, draft.active_message_id or 0, "stale-after-confirm")
        self.assertEqual(len(self.prediction().bets), 5)

    def test_correction_clone_hub_same_match_market_and_cancel(self):  # AC-52…AC-57
        self._confirmed_coupon(6)
        confirmed = self.prediction()
        self.message("/my")
        self._card_override = self.tg.messages[-1]
        self.click("✏️ Внести корректировки")
        draft = self.bot._draft("42")
        original = (dict(draft.selection_slots), [(bet.bet_id, bet.stake, tuple(event.match_id for event in bet.events)) for bet in draft.bets])
        self.assertEqual((draft.replacement_kind, len(draft.current_events), sum(bet.stake or 0 for bet in draft.bets)), ("correction", 6, 5000))
        self.message("/my")
        self.assertIn("✏️ Продолжить корректировки", self.labels())
        self.click("✏️ Продолжить корректировки")
        self.assertEqual(self.bot._draft("42").draft_id, draft.draft_id)
        self.click("⚽ Изменить события")
        self.open_match("M01")
        self.click_data(lambda data: data.endswith("market:M01:Х"))
        self.assertEqual(draft.phase, "E1")
        self.assertEqual(draft.selection_slots, original[0])
        self.assertEqual([(bet.bet_id, bet.stake, tuple(event.match_id for event in bet.events)) for bet in draft.bets], original[1])
        self.assertEqual(self.prediction(), confirmed)
        self.click("✖️")
        self.click("✖️ Удалить черновик")
        self.assertIsNone(self.bot._draft("42"))
        self.assertEqual(self.prediction(), confirmed)

    def test_full_replace_is_blank_and_distinct_from_correction(self):  # AC-58
        self._confirmed_coupon(6)
        confirmed = self.prediction()
        self.message("/my")
        self._card_override = self.tg.messages[-1]
        self.click("🔄 Заменить прогноз")
        self.assertIn("начнёте с пустого черновика", self.tg.messages[-1][2])
        self._card_override = self.tg.messages[-1]
        self.click("📝 Начать полную замену")
        draft = self.bot._draft("42")
        self.assertEqual((draft.replacement_kind, draft.current_events, draft.bets, draft.expresses), ("full", [], [], []))
        self.assertEqual(self.prediction(), confirmed)

    def test_correction_hub_routes_to_expresses_and_atomic_amount_edit(self):  # AC-53
        self._confirmed_coupon(6)
        self.message("/my"); self._card_override = self.tg.messages[-1]; self.click("✏️ Внести корректировки")
        self.click("🎯 Пересобрать экспрессы")
        self.assertEqual((self.bot._draft("42").phase, sum(bet.stake or 0 for bet in self.bot._draft("42").bets)), ("X2", 5000))
        # A fresh correction reaches B1 in edit mode; active five stakes are
        # retained until the existing B2 Apply contract accepts replacements.
        self.setUp(); self._confirmed_coupon(6)
        self.message("/my"); self._card_override = self.tg.messages[-1]; self.click("✏️ Внести корректировки")
        self.click("💰 Изменить суммы")
        draft = self.bot._draft("42")
        self.assertEqual((draft.phase, draft.stake_edit, sum(bet.stake or 0 for bet in draft.bets)), ("B1", [None] * 5, 5000))

    def test_v12_snapshot_restart_and_v2_backward_read(self):  # AC-50, AC-56
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory); repo.save_round(self.round_)
            tg = Telegram(); bot = BotService(repo, tg, lambda: self.now[0], draft_store=DraftStore(directory), composer_version=1)
            bot.handle_update(self.message_update("/start")); self._drive_confirm(bot, tg, 6)
            bot.handle_update(self.message_update("/my")); self._card_override = tg.messages[-1]; self._click_bot(bot, tg, "✏️ Внести корректировки")
            draft = bot._draft("42"); self._click_bot(bot, tg, "⚽ Изменить события"); self._open_bot(bot, tg, "M01"); self._click_bot(bot, tg, "🗑 Убрать матч"); self._click_bot(bot, tg, "✅ Убрать матч")
            restored = BotService(repo, tg, lambda: self.now[0], draft_store=DraftStore(directory), composer_version=1)._draft("42")
            self.assertEqual((restored.replacement_kind, restored.phase, len(restored.current_events)), ("correction", "E1", 5))

    def _confirmed_coupon(self, count: int) -> None:
        self.message("/predict")
        for number in range(1, count + 1): self.pick(f"M{number:02}")
        self.click("✅ Завершить"); self.click("🎯 Собрать экспрессы")
        if count == 7: self.click("4+1 · экспресс из 3")
        self.choose_express(3 if count == 7 else 2); self.click("✅ Экспресс готов"); self.click("💰 Распределить банк")
        for stake in (1000, 1000, 1000, 500, 1500): self.message(str(stake))
        self.click("✅ Подтвердить")

    def prediction(self):
        return self.repo.get_prediction(self.round_.round_id, self.repo.get_participant("42").participant_id)

    def open_match(self, match_id: str):
        while not any(button["callback_data"].endswith(f"match:{match_id}") for row in self.card()[3]["inline_keyboard"] for button in row): self.click("▶️")
        self.click_data(lambda data: data.endswith(f"match:{match_id}"))

    def pick(self, match_id: str):
        self.open_match(match_id); self.click_data(lambda data: data.endswith(":П1"))

    def choose_express(self, size: int):
        draft = self.bot._draft("42"); used = {event.match_id for group in draft.expresses for event in group}
        for event in [event for event in draft.current_events if event.match_id not in used][:size]: self.click_data(lambda data, event=event: data.endswith(f"x:t:{event.match_id}"))

    def card(self):
        if self._card_override:
            return self._card_override
        draft = self.bot._draft("42")
        if draft and draft.active_message_id is not None:
            for edit in reversed(self.tg.edits):
                if edit[1] == draft.active_message_id:
                    return edit
            for message in reversed(self.tg.messages):
                if message[1] == draft.active_message_id:
                    return message
        return self.tg.edits[-1] if self.tg.edits else self.tg.messages[-1]
    def labels(self): return [button["text"] for row in self.card()[3]["inline_keyboard"] for button in row]
    def label(self, prefix): return next(label for label in self.labels() if label.startswith(prefix))
    def button(self, label):
        for row in self.card()[3]["inline_keyboard"]:
            for button in row:
                if button["text"] == label: return button["callback_data"]
        self.fail(f"button {label!r} unavailable: {self.labels()}")
    def click(self, label):
        card = self.card(); draft = self.bot._draft("42")
        self.callback(self.button(label), draft.active_message_id if draft else card[1], label)
    def click_data(self, predicate):
        for row in self.card()[3]["inline_keyboard"]:
            for button in row:
                if predicate(button["callback_data"]): self.callback(button["callback_data"], self.bot._draft("42").active_message_id, "data"); return
        self.fail("callback unavailable")
    def callback(self, data, message_id, callback_id):
        self.bot.handle_update({"update_id": len(self.tg.answers) + 1, "callback_query": {"id": callback_id, "from": {"id": 42}, "data": data, "message": {"message_id": message_id, "chat": {"id": 1, "type": "private"}}}})
        self._card_override = None
    def message(self, text): self.bot.handle_update(self.message_update(text))
    @staticmethod
    def message_update(text): return {"message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 42, "first_name": "Т"}, "text": text}}

    # Small adapters keep the durable restart acceptance independent from the
    # in-memory fake used by the other CJM tests.
    def _drive_confirm(self, bot, tg, count):
        self.bot, self.tg = bot, tg; self._confirmed_coupon(count)
    def _click_bot(self, bot, tg, label): self.bot, self.tg = bot, tg; self.click(label)
    def _open_bot(self, bot, tg, match_id): self.bot, self.tg = bot, tg; self.open_match(match_id)
