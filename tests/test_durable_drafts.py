from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tak_ili_inache.bot import BotService
from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.delivery import UnknownDeliveryError
from tak_ili_inache.draft_store import DraftStore
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.telegram_api import TelegramHttpError


class _Telegram:
    def __init__(self): self.messages, self.edits, self.answers, self.clears, self.next_id, self.edit_failure, self.apply_edit_before_failure, self.send_failure, self.apply_send_before_failure, self.current = [], [], [], [], 100, None, False, None, False, None
    def send_message(self, chat_id, text, reply_markup=None):
        self.next_id += 1; message = (chat_id, self.next_id, text, reply_markup)
        if self.send_failure:
            error, self.send_failure = self.send_failure, None
            if self.apply_send_before_failure: self.messages.append(message); self.current = message
            self.apply_send_before_failure = False
            raise error
        self.messages.append(message); self.current = message; return self.next_id
    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        if self.edit_failure:
            error, self.edit_failure = self.edit_failure, None
            if self.apply_edit_before_failure: self.current = (chat_id, message_id, text, reply_markup); self.edits.append(self.current)
            self.apply_edit_before_failure = False
            raise error
        self.current = (chat_id, message_id, text, reply_markup); self.edits.append(self.current)
    def answer_callback(self, callback_id, text=""): self.answers.append((callback_id, text))
    def clear_keyboard(self, *args): self.clears.append(args)
    def get_updates(self, *args): return []
    def download_document(self, *args): return b""
    def send_document(self, *args): return None
    def send_photo(self, *args): return None
    def send_photo_bytes(self, *args): return None


class DurableDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.repo = CsvRepository(self.tmp.name)
        self.round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv"); self.repo.save_round(self.round_)
        self.now = [self.round_.deadline_msk - timedelta(minutes=10)]; self.tg, self.store = _Telegram(), DraftStore(self.tmp.name)
        self.bot = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=self.store); self.bot.handle_update(self._message("/start"))
    def tearDown(self): self.tmp.cleanup()
    def test_hundred_stale_replays_make_one_transition_and_keep_one_card(self):
        self.bot.handle_update(self._message("/predict")); draft = self.bot._draft("42"); action, card = self._button("Мексика — ЮАР"), draft.active_message_id
        self.bot.handle_update(self._callback(action, card, "first")); revision, edits = draft.revision, len(self.tg.edits)
        for index in range(100): self.bot.handle_update(self._callback(action, card, f"stale-{index}", index + 3))
        self.assertEqual((draft.revision, len(self.tg.edits), draft.active_message_id), (revision, edits, card)); self.assertEqual(sum(bool(text) for _, text in self.tg.answers), 100)
    def test_restart_snapshot_is_minimal_and_resumes_one_card(self):
        self.bot.handle_update(self._message("/predict")); draft = self.bot._draft("42"); self.bot.handle_update(self._callback(self._button("Мексика — ЮАР"), draft.active_message_id, "match"))
        restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name)); restored = restarted._draft("42")
        self.assertEqual((restored.draft_id, restored.active_message_id, restored.phase), (draft.draft_id, draft.active_message_id, "E2"))
        snapshot = json.loads((Path(self.tmp.name) / "drafts_runtime.json").read_text())["42"]
        self.assertEqual(snapshot["version"], 4); self.assertIn("expresses", snapshot); self.assertIn("stake_edit", snapshot); self.assertIn("selection_slots", snapshot); self.assertIn("reanchor_pending", snapshot); self.assertNotIn("token", json.dumps(snapshot).lower()); self.assertNotIn("payload", json.dumps(snapshot).lower())
        old_card = restored.active_message_id
        restarted.handle_update(self._message("/predict", 12))
        self.assertEqual((len(self.tg.messages), restarted._draft("42").active_message_id == old_card), (3, False))
    def test_duplicate_stale_and_edit_failure_are_safe(self):
        self.bot.handle_update(self._message("/predict")); draft = self.bot._draft("42"); card = draft.active_message_id; action = self._button("Мексика — ЮАР")
        self.bot.handle_update(self._callback(action, card, "one")); before = draft.revision; self.bot.handle_update(self._callback(action, card, "dup")); self.assertEqual(draft.revision, before)
        action, card = self._button("П1 · 1.80"), draft.active_message_id; self.tg.edit_failure = TelegramHttpError(400); self.bot.handle_update(self._callback(action, card, "fallback")); self.assertNotEqual(draft.active_message_id, card)

    def test_applied_edit_with_lost_response_never_sends_a_second_card(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        card, action = draft.active_message_id, self._button("Мексика — ЮАР")
        revision, sends, edits = draft.revision, len(self.tg.messages), len(self.tg.edits)
        self.tg.edit_failure = UnknownDeliveryError("response lost after applied edit")
        self.tg.apply_edit_before_failure = True

        with self.assertLogs("tak_ili_inache.bot", "WARNING") as captured:
            with self.assertRaises(UnknownDeliveryError):
                self.bot.handle_update(self._callback(action, card, "lost-response"))

        self.assertIn("telegram_draft_edit_unknown no_fallback=true", "\n".join(captured.output))
        self.assertEqual((draft.phase, draft.revision, draft.active_message_id), ("E2", revision + 1, card))
        self.assertEqual((len(self.tg.messages), len(self.tg.edits)), (sends, edits + 1))
        snapshot = json.loads((Path(self.tmp.name) / "drafts_runtime.json").read_text(encoding="utf-8"))["42"]
        self.assertEqual((snapshot["revision"], snapshot["active_message_id"], snapshot["phase"]), (revision + 1, card, "E2"))

        # The callback was durably consumed before the response was lost. Its
        # replay is stale, while explicit restart deliberately re-anchors once.
        self.bot.handle_update(self._callback(action, card, "replay"))
        self.assertEqual((draft.revision, len(self.tg.messages), len(self.tg.edits)), (revision + 1, sends, edits + 1))
        restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name))
        restarted.handle_update(self._message("/predict", 12))
        restored = restarted._draft("42")
        self.assertNotEqual(restored.active_message_id, card)
        self.assertEqual((len(self.tg.messages), len(self.tg.edits)), (sends + 1, edits + 1))

    def test_predict_reanchors_one_successor_and_makes_the_buried_card_stale(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        old_card, old_action = draft.active_message_id, self._button("Мексика — ЮАР")
        revision, sends, edits = draft.revision, len(self.tg.messages), len(self.tg.edits)

        self.bot.handle_update(self._message("/predict", 11))

        self.assertEqual((draft.revision, len(self.tg.messages), len(self.tg.edits)), (revision + 1, sends + 1, edits))
        self.assertNotEqual(draft.active_message_id, old_card)
        self.assertIn((42, old_card), self.tg.clears)
        stable = (draft.revision, draft.phase, draft.active_message_id, len(self.tg.messages))
        self.bot.handle_update(self._callback(old_action, old_card, "buried", 12))
        self.assertEqual((draft.revision, draft.phase, draft.active_message_id, len(self.tg.messages)), stable)
        self.assertEqual(self.tg.answers[-1][1], "Экран устарел. Откройте /predict.")

    def test_repeated_predict_keeps_only_the_latest_keyboard_live(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        first = draft.active_message_id
        self.bot.handle_update(self._message("/predict", 21))
        second = draft.active_message_id
        self.bot.handle_update(self._message("/predict", 22))
        third = draft.active_message_id

        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(draft.active_message_id, third)
        self.assertEqual(self.tg.clears[-2:], [(42, first), (42, second)])

    def test_my_reports_unconfirmed_draft_with_bound_resume_and_restart_confirmation(self):
        self.bot.handle_update(self._message("/my"))
        self.assertEqual(self.tg.current[2], "Прогноз ещё не подтверждён.")

        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        before = (draft.phase, draft.revision, draft.active_message_id)
        self.bot.handle_update(self._message("/my", 31))
        my_card = self.tg.current
        self.assertIn("Есть незавершённый черновик", my_card[2])
        self.assertIn("Этап: выбор матчей.", my_card[2])
        self.assertEqual((draft.phase, draft.revision, draft.active_message_id), before)
        buttons = {button["text"]: button["callback_data"] for row in my_card[3]["inline_keyboard"] for button in row}
        self.assertIn("▶️ Продолжить черновик", buttons)

        self.bot.handle_update(self._callback(buttons["🔄 Начать заново"], my_card[1], "restart-ask", 32))
        self.assertIs(self.bot._draft("42"), draft)
        self.assertIn("будет удалён только после подтверждения", self.tg.current[2])
        self.assertIn("🗑 Удалить и начать заново", [button["text"] for row in self.tg.current[3]["inline_keyboard"] for button in row])

    def test_resume_from_my_reanchors_when_an_old_edit_would_be_not_modified(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        old_card, sends, edits = draft.active_message_id, len(self.tg.messages), len(self.tg.edits)
        self.bot.handle_update(self._message("/my", 41))
        my_card = self.tg.current
        resume = next(button["callback_data"] for row in my_card[3]["inline_keyboard"] for button in row if button["text"] == "▶️ Продолжить черновик")
        self.tg.edit_failure = TelegramHttpError(400, "message_not_modified")

        self.bot.handle_update(self._callback(resume, my_card[1], "resume", 42))

        self.assertEqual((len(self.tg.messages), len(self.tg.edits)), (sends + 2, edits))
        self.assertNotEqual(draft.active_message_id, old_card)
        self.assertIn("Выберите матч", self.tg.current[2])

    def test_unknown_reanchor_send_is_not_retried_or_committed(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        old = (draft.revision, draft.active_message_id, len(self.tg.messages), len(self.tg.clears))
        self.tg.send_failure = UnknownDeliveryError("response lost after successor send")
        self.tg.apply_send_before_failure = True

        with self.assertRaises(UnknownDeliveryError):
            self.bot.handle_update(self._message("/predict", 51))

        self.assertEqual((draft.revision, draft.active_message_id, draft.reanchor_pending, len(self.tg.messages), len(self.tg.clears)), (old[0] + 1, None, True, old[2] + 1, old[3] + 1))
        snapshot = json.loads((Path(self.tmp.name) / "drafts_runtime.json").read_text(encoding="utf-8"))["42"]
        self.assertEqual((snapshot["revision"], snapshot["active_message_id"], snapshot["reanchor_pending"], snapshot["reanchor_predecessor_id"]), (old[0] + 1, None, True, old[1]))

    def test_reanchor_intent_survives_crash_after_successful_send_without_auto_retry(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        old_card, old_action, old_revision = draft.active_message_id, self._button("Мексика — ЮАР"), draft.revision
        original_save, intent_saved = self.store.save, [False]

        def crash_after_successor_intent(participant_id, snapshot):
            if snapshot["reanchor_pending"]:
                intent_saved[0] = True
            elif intent_saved[0] and snapshot["revision"] == old_revision + 1 and snapshot["active_message_id"] is not None:
                raise SystemExit("simulated process death after Telegram accepted successor")
            return original_save(participant_id, snapshot)

        self.store.save = crash_after_successor_intent
        with self.assertRaises(SystemExit):
            self.bot.handle_update(self._message("/predict", 55))
        self.store.save = original_save
        sends_after_crash = len(self.tg.messages)
        snapshot = json.loads((Path(self.tmp.name) / "drafts_runtime.json").read_text(encoding="utf-8"))["42"]
        self.assertEqual(
            (snapshot["revision"], snapshot["active_message_id"], snapshot["reanchor_pending"], snapshot["reanchor_predecessor_id"]),
            (old_revision + 1, None, True, old_card),
        )

        restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name))
        restored = restarted._draft("42")
        self.assertEqual((restored.revision, restored.active_message_id, restored.reanchor_pending), (old_revision + 1, None, True))
        self.assertEqual(len(self.tg.messages), sends_after_crash)
        restarted.handle_update(self._callback(old_action, old_card, "stale-after-crash", 56))
        self.assertEqual(len(self.tg.messages), sends_after_crash)
        self.assertEqual(self.tg.answers[-1][1], "Экран устарел. Откройте /predict.")

        restarted.handle_update(self._message("/my", 57))
        self.assertIn("Карточка восстановления не была подтверждена", self.tg.current[2])
        self.assertIn("Продолжить черновик", self.tg.current[2])

    def test_bound_resume_at_deadline_only_enters_l0_without_reanchor(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        old_card, old_revision = draft.active_message_id, draft.revision
        self.bot.handle_update(self._message("/my", 58))
        my_card = self.tg.current
        resume = next(button["callback_data"] for row in my_card[3]["inline_keyboard"] for button in row if button["text"] == "▶️ Продолжить черновик")
        sends, edits = len(self.tg.messages), len(self.tg.edits)
        self.now[0] = self.round_.deadline_msk

        self.bot.handle_update(self._callback(resume, my_card[1], "resume-at-deadline", 59))

        self.assertEqual((draft.phase, draft.revision, draft.active_message_id), ("L0", old_revision, old_card))
        self.assertEqual((len(self.tg.messages), len(self.tg.edits)), (sends, edits + 1))
        self.assertEqual(self.tg.edits[-1][1], old_card)
        self.assertIn("Дедлайн прошёл", self.tg.edits[-1][2])
        snapshot = json.loads((Path(self.tmp.name) / "drafts_runtime.json").read_text(encoding="utf-8"))["42"]
        self.assertEqual((snapshot["phase"], snapshot["revision"], snapshot["active_message_id"]), ("L0", old_revision, old_card))

        self.bot.handle_update(self._message("/my", 60))
        self.assertEqual((draft.phase, draft.revision, draft.active_message_id), ("L0", old_revision, old_card))
        self.assertIn("Дедлайн прошёл", self.tg.edits[-1][2])

    def test_reanchor_telemetry_is_hmac_correlated_without_a_raw_actor(self):
        self.bot.handle_update(self._message("/predict"))
        with patch.dict("os.environ", {"TAK_ILI_INACHE_TELEMETRY_HMAC_KEY": "unit-test-key"}, clear=False):
            with self.assertLogs("tak_ili_inache.bot", "INFO") as captured:
                self.bot.handle_update(self._message("/predict", "trace-61"))
        record = "\n".join(captured.output)
        self.assertIn("cjm_draft_trace actor=", record)
        self.assertIn("update_id=trace-61", record)
        self.assertIn("action=predict_reanchor", record)
        self.assertIn("draft_id=", record)
        self.assertIn("phase_before=E1", record)
        self.assertIn("card_from=", record)
        self.assertIn("card_to=", record)
        self.assertNotIn("actor=42", record)

    def test_match_page_boundaries_are_ack_only_and_transitions_keep_one_card(self):
        self.bot.handle_update(self._message("/predict"))
        draft = self.bot._draft("42")
        card = draft.active_message_id
        snapshot_path = Path(self.tmp.name) / "drafts_runtime.json"

        before = (draft.revision, draft.match_page, draft.active_message_id, len(self.tg.messages), len(self.tg.edits), snapshot_path.read_bytes())
        self.bot.handle_update(self._callback(self._button("◀️"), card, "first-boundary", 1))
        after = (draft.revision, draft.match_page, draft.active_message_id, len(self.tg.messages), len(self.tg.edits), snapshot_path.read_bytes())
        self.assertEqual(after, before)
        self.assertEqual(self.tg.answers[-1][1], "Вы уже на стр. 1/2.")

        revision, edits, messages = draft.revision, len(self.tg.edits), len(self.tg.messages)
        self.bot.handle_update(self._callback(self._button("▶️"), card, "to-second", 2))
        self.assertEqual((draft.match_page, draft.revision, draft.active_message_id), (1, revision + 1, card))
        self.assertEqual((len(self.tg.edits), len(self.tg.messages)), (edits + 1, messages))
        self.assertIn("Выберите матч · 2/2:", self.tg.edits[-1][2])

        self.bot = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name))
        draft = self.bot._draft("42")
        self.assertEqual((draft.match_page, draft.active_message_id), (1, card))

        before = (draft.revision, draft.match_page, draft.active_message_id, len(self.tg.messages), len(self.tg.edits), snapshot_path.read_bytes())
        self.bot.handle_update(self._callback(self._button("▶️"), card, "last-boundary", 3))
        after = (draft.revision, draft.match_page, draft.active_message_id, len(self.tg.messages), len(self.tg.edits), snapshot_path.read_bytes())
        self.assertEqual(after, before)
        self.assertEqual(self.tg.answers[-1][1], "Это последняя страница. Незанятые матчи — на стр. 1.")

        revision, edits = draft.revision, len(self.tg.edits)
        self.bot.handle_update(self._callback(self._button("◀️"), card, "to-first", 4))
        self.assertEqual((draft.match_page, draft.revision, draft.active_message_id), (0, revision + 1, card))
        self.assertEqual(len(self.tg.edits), edits + 1)
        self.assertIn("Выберите матч · 1/2:", self.tg.edits[-1][2])
    def test_deadline_turns_draft_read_only_and_crash_keeps_retryable_snapshot(self):
        self.bot.handle_update(self._message("/predict")); draft = self.bot._draft("42"); action, card = self._button("Мексика — ЮАР"), draft.active_message_id; original_save = self.store.save
        self.store.save = lambda *_: (_ for _ in ()).throw(OSError("snapshot crash"))
        with self.assertRaises(OSError): self.bot.handle_update(self._callback(action, card, "crash"))
        self.store.save = original_save; restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name)); self.assertEqual(restarted._draft("42").revision, 1)
        self.now[0] = self.round_.deadline_msk; restarted.handle_update(self._callback(action, card, "deadline")); self.assertEqual(restarted._draft("42").phase, "L0"); self.assertIn("⏰ Дедлайн прошёл", self.tg.edits[-1][2])

    def test_v1_snapshot_migrates_to_v11_without_losing_the_active_draft(self):
        self.bot.handle_update(self._message("/predict"))
        snapshot_path = Path(self.tmp.name) / "drafts_runtime.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))["42"]
        snapshot["version"] = 1; snapshot.pop("stake_edit"); snapshot.pop("stake_edit_index"); snapshot.pop("reanchor_pending"); snapshot.pop("reanchor_predecessor_id")
        snapshot_path.write_text(json.dumps({"42": snapshot}, ensure_ascii=False), encoding="utf-8")
        restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name))
        restored = restarted._draft("42")
        self.assertEqual((restored.phase, restored.stake_edit, restored.stake_edit_index), ("E1", None, 0))

    def test_v2_snapshot_migrates_to_v12_stable_identity_shape(self):
        self.bot.handle_update(self._message("/predict"))
        snapshot_path = Path(self.tmp.name) / "drafts_runtime.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))["42"]
        snapshot["version"] = 2
        snapshot.pop("replacement_kind")
        snapshot.pop("selection_slots")
        snapshot.pop("reanchor_pending")
        snapshot.pop("reanchor_predecessor_id")
        snapshot_path.write_text(json.dumps({"42": snapshot}, ensure_ascii=False), encoding="utf-8")
        restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name))
        restored = restarted._draft("42")
        self.assertEqual((restored.replacement_kind, restored.selection_slots, restored.phase), ("new", {}, "E1"))
    def _button(self, label):
        markup = self.tg.current[3]
        for row in markup["inline_keyboard"]:
            for button in row:
                if button["text"] == label or label in button["text"]:
                    return button["callback_data"]
        self.fail(f"button {label!r} not found")
    @staticmethod
    def _message(text, update_id=0): return {"update_id": update_id, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Т"}, "text": text}}
    @staticmethod
    def _callback(data, message_id, callback_id, update_id=0): return {"update_id": update_id, "callback_query": {"id": callback_id, "from": {"id": 42}, "data": data, "message": {"message_id": message_id, "chat": {"id": 42, "type": "private"}}}}
