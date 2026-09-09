from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.bot import BotService
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures


class Telegram:
    def __init__(self): self.messages, self.edits, self.answers, self.message_id, self.current = [], [], [], 0, None
    def send_message(self, chat_id, text, reply_markup=None):
        self.message_id += 1; self.current = (chat_id, self.message_id, text, reply_markup); self.messages.append(self.current); return self.message_id
    def edit_message(self, chat_id, message_id, text, reply_markup=None): self.current = (chat_id, message_id, text, reply_markup); self.edits.append(self.current)
    def answer_callback(self, callback_id, text=""): self.answers.append((callback_id, text))
    def clear_keyboard(self, *args): pass
    def download_document(self, *_): return b""
    def send_document(self, *_): return None
    def send_photo(self, *_): return None
    def send_photo_bytes(self, *_): return None


class CjmV1Tests(unittest.TestCase):
    def setUp(self):
        self.repo, self.tg = FakeRepository(), Telegram()
        self.round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
        self.repo.save_round(self.round_); self.now = [self.round_.deadline_msk - timedelta(minutes=10)]
        self.bot = BotService(self.repo, self.tg, lambda: self.now[0], admin_ids={"99"})
        self.message("/start")

    def test_event_first_finishes_at_six_and_caps_at_nine(self):  # AC-01…AC-06
        self.message("/predict")
        self.assertIn("Выбрано событий/вариантов: 0 из 6–9", self.card()[2])
        self.assertNotIn("4+1", self.card()[2])
        for match in ("M01", "M02", "M03", "M04", "M05"):
            self.pick(match)
        self.assertNotIn("Завершить выбор", self.labels())
        self.pick("M06"); self.assertIn("✅ Завершить", self.labels())
        # A chosen match may keep alternatives, but the draft cannot advance
        # while it contains more than one outcome for that match.
        before = len(self.bot._draft("42").current_events)
        while not any(button["callback_data"].endswith("match:M01") for row in self.card()[3]["inline_keyboard"] for button in row): self.click("◀️")
        self.click_data(lambda value: value.endswith("match:M01")); self.click_data(lambda value: value.endswith(":Х"))
        self.assertEqual(len(self.bot._draft("42").current_events), before + 1)
        self.click("✅ Завершить")
        self.assertIn("оставьте по одному исходу", self.tg.messages[-1][2])
        while not any(button["callback_data"].endswith("match:M01") for row in self.card()[3]["inline_keyboard"] for button in row): self.click("◀️")
        self.click_data(lambda value: value.endswith("match:M01")); self.click_data(lambda value: value.endswith(":П1"))
        self.assertEqual(len(self.bot._draft("42").current_events), before)
        for match in ("M07", "M08", "M09"): self.pick(match)
        self.assertEqual(len(self.bot._draft("42").current_events), 9)
        self.click_data(lambda value: value.endswith("match:M10"))
        self.assertIn("Уже выбрано 9 событий", self.tg.messages[-1][2])

    def test_exact_schemes_for_6_7_8_9_events_and_unique_expresses(self):  # AC-07…AC-12, AC-32
        for count, expected, sizes in ((6, (4, 1), [2]), (7, (4, 1), [3]), (8, (3, 2), [2, 3]), (9, (3, 2), [3, 3])):
            with self.subTest(count=count):
                self.setUp(); self.message("/predict")
                for number in range(1, count + 1): self.pick(f"M{number:02}")
                self.click("✅ Завершить"); self.click("🎯 Собрать экспрессы")
                if count == 7: self.click("4+1 · экспресс из 3")
                draft = self.bot._draft("42"); self.assertEqual(draft.structure, expected)
                for size in sizes:
                    self.choose_express(size)
                    expected_odds = __import__("functools").reduce(lambda value, event: value * event.odds_snapshot, draft.expresses[-1], __import__("decimal").Decimal("1"))
                    self.assertIn(f"Итоговый кэф сейчас: {expected_odds}", self.card()[2])
                    self.click("✅ Экспресс готов")
                self.assertEqual([len(item) for item in draft.expresses], sizes)
                self.assertEqual(len({event.match_id for group in draft.expresses for event in group}), sum(sizes))
                self.click("🗑 Сбросить экспресс 1"); self.assertEqual(draft.expresses[0], [])
                self.click("← Назад"); self.assertEqual(len(draft.current_events), count)

    def test_bank_recommendations_manual_preview_and_confirm(self):  # AC-13…AC-17
        self._six_to_review(); self.click("💰 Распределить банк")
        self.assertLessEqual(len(self.labels()), 7); self.assertIn("Остаток: 5000", self.card()[2])
        self.message("1000"); self.message("1000"); self.message("1000"); self.message("500"); self.message("1500")
        self.assertIn("🔎 Проверьте прогноз", self.card()[2]); self.assertNotIn("купон", self.card()[2].lower())
        self.click("✅ Подтвердить"); participant = self.repo.get_participant("42")
        prediction = self.repo.get_prediction("R1", participant.participant_id)
        self.assertEqual((len(prediction.bets), sum(bet.stake for bet in prediction.bets)), (5, 5000))

    def test_forced_final_amount_has_exact_quick_button(self):
        self._six_to_review(); self.click("💰 Распределить банк")
        for stake in (1000, 1000, 1000, 1300):
            self.message(str(stake))
        self.assertIn("Допустимо: 700–700, шаг 50", self.card()[2])
        self.assertIn("✅ Выбрать 700", self.labels())
        self.click("✅ Выбрать 700")
        draft = self.bot._draft("42")
        self.assertEqual((draft.phase, sum(bet.stake or 0 for bet in draft.bets)), ("P1", 5000))
        self.assertIn("🔎 Проверьте прогноз", self.card()[2])

    def test_manual_amount_uses_the_rendered_dynamic_range(self):
        self._six_to_review(); self.click("💰 Распределить банк")
        for stake in (1000, 1000, 1000, 1300):
            self.message(str(stake))
        draft = self.bot._draft("42")
        before = (draft.revision, draft.phase, tuple(bet.stake for bet in draft.bets), draft.active_message_id)
        for invalid in (449, 550):
            self.message(str(invalid))
            self.assertEqual(self.tg.messages[-1][2], "Введите ровно 700 — это единственная допустимая сумма.")
            self.assertEqual((draft.revision, draft.phase, tuple(bet.stake for bet in draft.bets), draft.active_message_id), before)
        self.message("700")
        self.assertEqual((draft.phase, sum(bet.stake or 0 for bet in draft.bets)), ("P1", 5000))
        self.assertIn("🔎 Проверьте прогноз", self.card()[2])

    def test_single_and_full_express_rebuild_preserve_events(self):  # AC-11, AC-12
        self._six_to_review(); draft = self.bot._draft("42"); selected = tuple(draft.current_events)
        self.click("🗑 Сбросить экспресс 1"); self.assertEqual(draft.expresses[0], [])
        self.assertEqual(tuple(draft.current_events), selected)
        self.choose_express(2); self.click("✅ Экспресс готов"); self.click("🔄 Пересобрать экспрессы")
        self.assertEqual(tuple(draft.current_events), selected); self.assertTrue(all(not group for group in draft.expresses))

    def test_preview_help_admin_my_copy_and_button_limits(self):  # AC-17, AC-22…AC-25
        self.message("/help"); self.assertEqual(self.tg.messages[-1][2], self.bot._help())
        self.now[0] = self.round_.deadline_msk - timedelta(minutes=10); self.message("/admin", user="99")
        self.assertIn("🛠️ Админские команды", self.tg.messages[-1][2])
        self.message("/predict"); self.assertLessEqual(len(self.labels()), 15)

    def test_back_cancel_resume_and_atomic_replacement(self):  # AC-18…AC-21, AC-26, AC-28, AC-30, AC-31
        self.message("/predict"); self.pick("M01"); self.click_data(lambda value: value.endswith("match:M01")); self.click("← Назад")
        self.assertEqual(len(self.bot._draft("42").current_events), 1)
        self.click("✖️"); self.assertEqual(self.bot._draft("42").phase, "C0"); self.click("Продолжить")
        self.assertEqual(self.bot._draft("42").phase, "E1")
        self.click("✖️"); self.click("✖️ Удалить черновик"); self.assertIsNone(self.bot._draft("42"))

    def test_deadline_turns_draft_read_only(self):  # AC-27, AC-29, AC-33
        self.message("/predict"); self.pick("M01")
        self.now[0] = self.round_.deadline_msk; self.message("/predict")
        self.assertEqual(self.bot._draft("42").phase, "L0"); self.assertIn("⏰ Дедлайн прошёл", self.card()[2]); self.assertEqual(self.labels(), ["🔒 Мой прогноз", "✖️ Закрыть"])

    def test_v11_compact_pages_events_edit_warning_and_limits(self):  # AC-02,12,13,17,19,24,34…42
        self.message("/predict")
        markup = self.card()[3]["inline_keyboard"]
        self.assertEqual([button["text"] for button in markup[-1]], ["◀️", "▶️", "✅ 6+ событий", "✖️"])
        self.assertTrue(all(len(row) <= 2 for row in markup[:-1]))
        self.assertTrue(all("M0" not in button["text"] for row in markup for button in row))
        self.click_data(lambda value: value.endswith("match:M01"))
        self.assertIn("Можно выбрать до трёх вариантов", self.card()[2]); self.assertNotIn("рынок", self.card()[2].lower())
        self.assertEqual(sum(1 for label in self.labels() if "·" in label), 7)
        self.assertNotIn("page:e", str(self.card()[3])); self.assertLessEqual(len(self.labels()), 9)
        self.click_data(lambda value: value.endswith("market:M01:ТБ"))
        for number in range(2, 7): self.pick(f"M{number:02}")
        self.click("✅ Завершить"); self.click("🎯 Собрать экспрессы")
        x1_labels = self.labels()
        self.assertEqual(len([label for label in x1_labels if label.startswith("▫️")]), 6)
        self.assertTrue(all("M0" not in label for label in x1_labels))
        self.choose_express(2); self.click("✅ Экспресс готов")
        self.click("💰 Распределить банк")
        self.assertNotIn("M01", self.card()[2]); self.assertIn("Текущая сумма", self.card()[2]); self.assertIn("Допустимо", self.card()[2])
        for stake in (1000, 1000, 1000, 500, 1500): self.message(str(stake))
        preview = self.card()[2]
        self.assertNotIn("Коэффициент", preview); self.assertNotIn("Итого ставок", preview); self.assertNotIn("💰 Банк", preview)
        previous = [bet.stake for bet in self.bot._draft("42").bets]
        self.click("💰 Изменить суммы")
        self.assertEqual(self.bot._draft("42").stake_edit, [None] * 5)
        self.assertIn("1 000", self.labels())
        for stake in (500, 1000, 1000, 1000, 1500): self.message(str(stake))
        self.assertEqual(self.bot._draft("42").phase, "B2")
        self.click("✖️ Отменить изменение")
        self.assertEqual([bet.stake for bet in self.bot._draft("42").bets], previous)
        self.click("← Назад"); self.click("← Назад")
        self.click_data(lambda value: value.endswith("match:M01")); self.click_data(lambda value: value.endswith("market:M01:Х"))
        # Same-match market replacement is compatible: it keeps the coupon's
        # stable identities and stake vector instead of a destructive reset.
        self.assertEqual(self.bot._draft("42").phase, "E1")
        self.assertEqual([bet.stake for bet in self.bot._draft("42").bets], previous)
        self.assertTrue(all(len(button["callback_data"]) <= 64 for row in self.card()[3]["inline_keyboard"] for button in row))
        self.assertLessEqual(len(self.labels()), 15)

    def test_v11_rebuild_reset_schema_warning_and_atomic_apply(self):  # AC-12,19,37…42
        self.message("/predict")
        for number in range(1, 8): self.pick(f"M{number:02}")
        self.click("✅ Завершить"); self.click("🎯 Собрать экспрессы"); self.click("4+1 · экспресс из 3")
        self.choose_express(3); self.click("✅ Экспресс готов"); self.click("💰 Распределить банк")
        for stake in (1000, 1000, 1000, 500, 1500): self.message(str(stake))
        original = [bet.stake for bet in self.bot._draft("42").bets]
        self.click("← Назад")
        self.click("🗑 Сбросить экспресс 1"); self.choose_express(3); self.click("✅ Экспресс готов")
        self.assertEqual([bet.stake for bet in self.bot._draft("42").bets], original)
        self.click("🔄 Пересобрать экспрессы"); self.click("4+1 · экспресс из 3")
        self.choose_express(3); self.click("✅ Экспресс готов")
        self.assertEqual([bet.stake for bet in self.bot._draft("42").bets], original)
        self.click("↔️ Сменить схему"); self.click("3+2 · два экспресса по 2")
        self.assertEqual(self.bot._draft("42").phase, "W1")
        self.click("← Оставить без изменений")
        self.assertEqual([bet.stake for bet in self.bot._draft("42").bets], original)
        self.click("3+2 · два экспресса по 2"); self.click("✅ Продолжить и сбросить суммы")
        draft = self.bot._draft("42")
        self.assertEqual((draft.structure, draft.bets, draft.stake_edit), ((3, 2), [], None))

    def _six_to_review(self):
        self.message("/predict")
        for number in range(1, 7): self.pick(f"M{number:02}")
        self.click("✅ Завершить"); self.click("🎯 Собрать экспрессы")
        self.choose_express(2); self.click("✅ Экспресс готов")
    def pick(self, match):
        while not any(button["callback_data"].endswith(f"match:{match}") for row in self.card()[3]["inline_keyboard"] for button in row): self.click("▶️")
        self.click_data(lambda value: value.endswith(f"match:{match}"))
        self.click_data(lambda value: value.endswith(":П1"))
    def choose_express(self, size):
        draft = self.bot._draft("42")
        used = {event.match_id for group in draft.expresses for event in group}
        for event in [item for item in draft.current_events if item.match_id not in used][:size]:
            self.click_data(lambda value, event=event: value.endswith(f"x:t:{event.match_id}"))
    def card(self): return self.tg.current
    def labels(self): return [button["text"] for row in self.card()[3]["inline_keyboard"] for button in row]
    def click(self, label):
        for row in self.card()[3]["inline_keyboard"]:
            for button in row:
                if button["text"] == label:
                    self.bot.handle_update({"update_id": len(self.tg.answers) + 1, "callback_query": {"id": str(len(self.tg.answers)), "from": {"id": 42}, "data": button["callback_data"], "message": {"message_id": self.bot._draft("42").active_message_id, "chat": {"id": 1, "type": "private"}}}}); return
        self.fail(f"button {label!r} unavailable: {self.labels()}")
    def click_data(self, predicate):
        for row in self.card()[3]["inline_keyboard"]:
            for button in row:
                if predicate(button["callback_data"]):
                    self.bot.handle_update({"update_id": len(self.tg.answers) + 1, "callback_query": {"id": str(len(self.tg.answers)), "from": {"id": 42}, "data": button["callback_data"], "message": {"message_id": self.bot._draft("42").active_message_id, "chat": {"id": 1, "type": "private"}}}}); return
        self.fail("callback not found")
    def message(self, text, user="42"):
        self.bot.handle_update({"message": {"chat": {"id": 1, "type": "private"}, "from": {"id": int(user), "first_name": "Т"}, "text": text}})
