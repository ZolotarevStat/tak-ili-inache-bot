from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tak_ili_inache.bot import BotService
from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.delivery import UnknownDeliveryError
from tak_ili_inache.draft_store import DraftStore
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.telegram_api import TelegramHttpError


class _Telegram:
    def __init__(self): self.messages, self.edits, self.answers, self.next_id, self.edit_failure, self.apply_edit_before_failure = [], [], [], 100, None, False
    def send_message(self, chat_id, text, reply_markup=None): self.next_id += 1; self.messages.append((chat_id, self.next_id, text, reply_markup)); return self.next_id
    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        if self.edit_failure:
            error, self.edit_failure = self.edit_failure, None
            if self.apply_edit_before_failure: self.edits.append((chat_id, message_id, text, reply_markup))
            self.apply_edit_before_failure = False
            raise error
        self.edits.append((chat_id, message_id, text, reply_markup))
    def answer_callback(self, callback_id, text=""): self.answers.append((callback_id, text))
    def clear_keyboard(self, *args): pass
    def get_updates(self, *args): return []
    def download_document(self, *args): return b""
    def send_document(self, *args): return None
    def send_photo(self, *args): return None


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
        self.assertEqual(snapshot["version"], 3); self.assertIn("expresses", snapshot); self.assertIn("stake_edit", snapshot); self.assertIn("selection_slots", snapshot); self.assertNotIn("token", json.dumps(snapshot).lower()); self.assertNotIn("payload", json.dumps(snapshot).lower())
        restarted.handle_update(self._message("/predict", 12)); self.assertEqual(len(self.tg.messages), 2)
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
        # replay is stale, while restart resumes by editing the same card.
        self.bot.handle_update(self._callback(action, card, "replay"))
        self.assertEqual((draft.revision, len(self.tg.messages), len(self.tg.edits)), (revision + 1, sends, edits + 1))
        restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name))
        restarted.handle_update(self._message("/predict", 12))
        restored = restarted._draft("42")
        self.assertEqual((restored.active_message_id, len(self.tg.messages), len(self.tg.edits)), (card, sends, edits + 2))

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
        snapshot["version"] = 1; snapshot.pop("stake_edit"); snapshot.pop("stake_edit_index")
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
        snapshot_path.write_text(json.dumps({"42": snapshot}, ensure_ascii=False), encoding="utf-8")
        restarted = BotService(self.repo, self.tg, lambda: self.now[0], draft_store=DraftStore(self.tmp.name))
        restored = restarted._draft("42")
        self.assertEqual((restored.replacement_kind, restored.selection_slots, restored.phase), ("new", {}, "E1"))
    def _button(self, label):
        markup = self.tg.edits[-1][3] if self.tg.edits else self.tg.messages[-1][3]
        for row in markup["inline_keyboard"]:
            for button in row:
                if button["text"] == label or label in button["text"]:
                    return button["callback_data"]
        self.fail(f"button {label!r} not found")
    @staticmethod
    def _message(text, update_id=0): return {"update_id": update_id, "message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Т"}, "text": text}}
    @staticmethod
    def _callback(data, message_id, callback_id, update_id=0): return {"update_id": update_id, "callback_query": {"id": callback_id, "from": {"id": 42}, "data": data, "message": {"message_id": message_id, "chat": {"id": 42, "type": "private"}}}}
