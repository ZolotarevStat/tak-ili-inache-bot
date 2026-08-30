from __future__ import annotations

import csv
import unittest
from datetime import timedelta
from pathlib import Path
import tempfile

from tak_ili_inache.bot import BotService
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict | None]] = []
        self.answered: list[str] = []
        self.cleared: list[tuple[int, int]] = []
        self.documents: list[tuple[int, str, str]] = []
        self.document_parse_modes: list[str | None] = []
        self.photos: list[tuple[int, str, str]] = []
        self.photo_bytes: list[tuple[int, str, bytes, str]] = []
        self.edits: list[tuple[int, int, str, dict | None]] = []
        self._message_id = 0
        self.send_calls = 0

    def send_message(self, chat_id, text, reply_markup=None) -> None:
        self.send_calls += 1
        self.messages.append((chat_id, text, reply_markup))
        self._message_id += 1
        return self._message_id

    def edit_message(self, chat_id, message_id, text, reply_markup=None) -> None:
        self.edits.append((chat_id, message_id, text, reply_markup))
        self.messages.append((chat_id, text, reply_markup))

    def answer_callback(self, callback_id, text="") -> None:
        self.answered.append(callback_id)

    def clear_keyboard(self, chat_id, message_id) -> None:
        self.cleared.append((chat_id, message_id))

    def get_updates(self, offset, timeout):
        return []

    def download_document(self, document):
        return document["content"]

    def send_document(self, chat_id, path, caption="", parse_mode=None):
        self.documents.append((chat_id, path, caption))
        self.document_parse_modes.append(parse_mode)

    def send_photo(self, chat_id, path, caption=""):
        self.photos.append((chat_id, path, caption))

    def send_photo_bytes(self, chat_id, filename, content, caption=""):
        self.photo_bytes.append((chat_id, filename, content, caption))
        self.photos.append((chat_id, filename, caption))


class BotFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.tg = FakeRepository(), FakeTelegram()
        self.round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
        self.repo.save_round(self.round_)
        self.now = self.round_.deadline_msk - timedelta(minutes=10)
        self.bot = BotService(self.repo, self.tg, lambda: self.now)

    def test_private_happy_path_registers_saves_and_replaces_prediction(self) -> None:
        self._message("/start")
        self.assertIsNotNone(self.repo.get_participant("42"))
        self.tg.answer_callback = lambda callback_id: (_ for _ in ()).throw(OSError("test-only expired callback"))
        self._callback("menu:new")
        self.assertIn("Выбрано событий: 0 из 6–9", self.tg.messages[-1][1])
        self.tg.answer_callback = lambda callback_id: self.tg.answered.append(callback_id)
        for match_id in ["M01", "M02", "M03", "M04", "M05", "M06"]: self._event(match_id)
        self._callback("finish-events")
        self._callback("to-express")
        self._callback("x:t:M01"); self._callback("x:t:M02"); self._callback("x:done")
        self._callback("bank")
        for stake in [1000, 1000, 1000, 500, 1500]:
            self._callback(f"stake:{stake}")
        self.assertIn("Проверьте прогноз", self.tg.messages[-1][1])
        self._callback("confirm")
        participant = self.repo.get_participant("42")
        self.assertEqual(len(self.repo.raw_predictions()), 1)
        self.assertEqual(len(self.repo.get_prediction("R1", participant.participant_id).bets), 5)
        self._message("/my")
        self.assertIn("🔒 Мой прогноз", self.tg.messages[-1][1])
        # Replacement is explicit; latest changes and raw history stays append-only.
        self._callback("replace")
        self._callback("full:start")
        for match_id in ["M01", "M02", "M03", "M04", "M05", "M06"]: self._event(match_id)
        self._callback("finish-events"); self._callback("to-express")
        self._callback("x:t:M01"); self._callback("x:t:M02"); self._callback("x:done")
        self._callback("bank")
        for stake in [1000, 1000, 1000, 500, 1500]:
            self._callback(f"stake:{stake}")
        self._callback("confirm")
        self.assertEqual(len(self.repo.raw_predictions()), 2)
        self._callback("replace")
        self._callback("full:start")
        self._event("M07")
        self._callback("cancel")
        self._callback("cancel:yes")
        self.assertGreater(len(self.tg.cleared), 0)

    def test_group_blocks_sensitive_actions_and_deadline_blocks_replace(self) -> None:
        self.bot.handle_update({"message": {"chat": {"id": -9, "type": "group"}, "from": {"id": 42, "first_name": "Т"}, "text": "/start"}})
        self.assertIsNone(self.repo.get_participant("42"))
        self.assertIn("личном", self.tg.messages[-1][1])
        self.now = self.round_.deadline_msk
        self._message("/start")
        self._callback("menu:new")
        self.assertIn("прошёл", self.tg.messages[-1][1])

    def test_group_public_commands_accept_bot_suffix_and_ordinary_text_is_silent(self) -> None:
        group = {"id": -9, "type": "group"}
        user = {"id": 42, "first_name": "Т"}
        self.bot.handle_update({"message": {"chat": group, "from": user, "text": "/help@tii_example_bot"}})
        self.assertIn("❓ Помощь", self.tg.messages[-1][1])
        self.bot.handle_update({"message": {"chat": group, "from": user, "text": "/rules@tii_example_bot"}})
        self.assertIn("Ровно 5 ставок", self.tg.messages[-1][1])
        message_count = len(self.tg.messages)
        self.bot.handle_update({"message": {"chat": group, "from": user, "text": "обычное сообщение"}})
        self.assertEqual(len(self.tg.messages), message_count)
        self.bot.handle_update({"message": {"chat": group, "from": user, "sticker": {"file_id": "test-only"}}})
        self.assertEqual(len(self.tg.messages), message_count)
        self.bot.handle_update({"message": {"chat": group, "from": user, "text": "/predict@tii_example_bot"}})
        self.assertEqual(len(self.tg.messages), message_count + 1)
        self.assertIn("личном", self.tg.messages[-1][1])
        self.assertIsNone(self.repo.get_participant("42"))

    def test_base_commands_route_while_a_user_draft_is_active(self) -> None:
        self._message("/start")
        self._callback("menu:new")
        self._callback("structure:4+1")
        self._callback("match:M01")
        self.assertIsNotNone(self.bot._draft("42"))
        self._message("/help")
        self.assertIn("❓ Помощь", self.tg.messages[-1][1])
        self._message("/my")
        self.assertIn("Есть незавершённый черновик", self.tg.messages[-1][1])
        self._message("/start")
        self.assertIn("Регистрация готова", self.tg.messages[-1][1])
        self.assertIsNotNone(self.bot._draft("42"))
        self.bot.admin_ids.add("99")
        self.bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "text": "/admin"}})
        self.assertIn("🛠️ Админские команды", self.tg.messages[-1][1])

    def test_dashboards_only_show_actions_available_in_current_state(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        bot = BotService(repo, tg, lambda: self.now, admin_ids={"99"})
        bot.handle_update({"message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 42, "first_name": "Тестер"}, "text": "/start"}})
        self.assertIsNone(tg.messages[-1][2])
        bot.handle_update({"message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 42, "first_name": "Тестер"}, "text": "/my"}})
        self.assertEqual(tg.messages[-1][1], "Вы зарегистрированы. Сейчас нет открытого тура.")
        bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "text": "/admin"}})
        labels = _labels(tg.messages[-1][2])
        self.assertEqual(labels, ["Администраторы", "Формат и пример CSV"])
        self._admin_callback(bot, tg, "admin:csv-format")
        self.assertEqual(tg.documents[-1][2], "fixtures_example.csv")
        self.assertIn("round_id", tg.messages[-2][1])
        self._admin_callback(bot, tg, "admin:menu")
        self.assertEqual(_labels(tg.messages[-1][2]), ["Администраторы", "Формат и пример CSV"])

    def test_admin_menu_exposes_status_publish_results_and_scoring_by_stage(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        now = [self.round_.deadline_msk - timedelta(minutes=10)]
        bot = BotService(repo, tg, lambda: now[0], admin_ids={"99"}, tournament_chat_id=-100)
        repo.save_round(self.round_)

        bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "text": "/admin"}})
        labels = _labels(tg.messages[-1][2])
        self.assertIn("Статус сдачи", labels)
        self.assertNotIn("Опубликовать прогнозы", labels)

        now[0] = self.round_.deadline_msk
        bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "text": "/admin"}})
        labels = _labels(tg.messages[-1][2])
        self.assertIn("Статус сдачи", labels)
        self.assertIn("Опубликовать прогнозы", labels)
        self.assertIn("Внести результаты", labels)
        self.assertNotIn("Скоринг", labels)

        result = __import__("tak_ili_inache.models", fromlist=["BetResult", "Market"])
        for fixture in self.round_.fixtures:
            repo.save_result(result.BetResult(fixture.match_id, frozenset({result.Market.P1, result.Market.ONE_X, result.Market.TB})))
        bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "text": "/admin"}})
        labels = _labels(tg.messages[-1][2])
        self.assertIn("Опубликовать прогнозы", labels)
        self.assertIn("Скоринг", labels)
        self.assertNotIn("Внести результаты", labels)

    def test_admin_synthetic_tour_import_publish_results_and_score(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        now = [self.round_.deadline_msk - timedelta(minutes=10)]
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(repo, tg, lambda: now[0], admin_ids={"99"}, tournament_chat_id=-100, output_dir=output)
            content = (Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv").read_bytes()
            tg.download_document = lambda document: (_ for _ in ()).throw(OSError("test-only download failure"))
            bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "document": {"file_name": "fixtures.csv", "content": content}}})
            self.assertIn("Не удалось скачать CSV", tg.messages[-1][1])
            tg.download_document = lambda document: document["content"]
            bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "document": {"file_name": "fixtures.csv", "content": content}}})
            self.assertIn("Проверка пройдена", tg.messages[-1][1])
            self.assertNotIn("checksum", tg.messages[-1][1])
            self._admin_callback(bot, tg, "admin:activate")
            self.assertIsNotNone(repo.get_active_round())
            bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "document": {"file_name": "fixtures.csv", "content": content}}})
            self.assertIn("Перезаписать активный тур", _labels(tg.messages[-1][2]))
            self._admin_callback(bot, tg, "admin:replace")
            self.assertIn("перезаписан", tg.messages[-1][1])
            participant = repo.register_participant("42", "Игрок")
            base = __import__("test_validators").valid_prediction()
            repo.save_prediction(base.__class__(base.round_id, participant.participant_id, base.bets, base.submitted_at_msk))
            bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "document": {"file_name": "fixtures.csv", "content": content}}})
            self._admin_callback(bot, tg, "admin:replace")
            self.assertIn("Перезапись заблокирована", tg.messages[-1][1])
            now[0] = self.round_.deadline_msk
            self._admin_callback(bot, tg, "admin:publish")
            self.assertEqual(len([item for item in tg.documents if item[0] == -100]), 1)
            self.assertEqual(len([item for item in tg.photos if item[0] == -100]), 1)
            published_count = len(tg.documents) + len(tg.photos)
            self._admin_callback(bot, tg, "admin:publish")
            self.assertEqual(len(tg.documents) + len(tg.photos), published_count)
            self._admin_callback(bot, tg, "admin:score")
            self.assertIn("заблокирован", tg.messages[-1][1])
            for fixture in self.round_.fixtures:
                self._admin_callback(bot, tg, f"admin:result:{fixture.match_id}")
                if fixture.match_id == "M01":
                    self._admin_callback(bot, tg, f"admin:return:{fixture.match_id}")
                else:
                    self._admin_callback(bot, tg, f"admin:home-goals:{fixture.match_id}:4")
                    self._admin_callback(bot, tg, f"admin:away-goals:{fixture.match_id}:1")
            self.assertEqual(len(repo.results("R1")[0].returned_markets), 7)
            self._admin_callback(bot, tg, "admin:score")
            self.assertEqual(len(tg.documents), 4)
            self.assertTrue(all(Path(path).exists() for _, path, _ in tg.documents))
            self.assertEqual(len(tg.photos), 3)
            self.assertTrue(all(path.endswith(".png") for _, path, _ in tg.photos))
            self.assertFalse(any(Path(path).exists() for _, path, _ in tg.photos), tg.photos)
            self.assertEqual(
                {caption for _, _, caption in tg.photos},
                {
                    tg.photos[0][2],
                    "Итоговый рейтинг валовых выплат",
                    "",
                },
            )
            self.assertIn("сверены", tg.messages[-1][1])
            document_count = len(tg.documents)
            photo_count = len(tg.photos)
            self._admin_callback(bot, tg, "admin:score")
            self.assertEqual(len(tg.documents), document_count)
            self.assertEqual(len(tg.photos), photo_count)

    def test_admin_score_entry_edits_one_card_and_derives_markets(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        bot = BotService(repo, tg, lambda: self.round_.deadline_msk, admin_ids={"99"})
        bot.handle_update({"message": {"chat": {"id": 9, "type": "private"}, "from": {"id": 99}, "text": "/admin"}})
        sent_before = tg.send_calls
        cleared_before = len(tg.cleared)

        self._admin_callback(bot, tg, "admin:results")
        self._admin_callback(bot, tg, "admin:result:M01")
        self._admin_callback(bot, tg, "admin:home-goals:M01:4")
        self._admin_callback(bot, tg, "admin:away-goals:M01:1")

        self.assertEqual(tg.send_calls, sent_before)
        self.assertEqual(len(tg.cleared), cleared_before)
        self.assertEqual(len(tg.edits), 4)
        result = repo.results("R1")[0]
        self.assertEqual(result.winning_markets, frozenset({__import__("tak_ili_inache.models", fromlist=["Market"]).Market.P1, __import__("tak_ili_inache.models", fromlist=["Market"]).Market.ONE_X, __import__("tak_ili_inache.models", fromlist=["Market"]).Market.TB}))
        self.assertIn("✅", tg.edits[-1][2])
        self.assertIn("4:1", tg.edits[-1][2])

    def test_admin_five_plus_score_asks_outcome_and_full_return_is_immediate(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        bot = BotService(repo, tg, lambda: self.round_.deadline_msk, admin_ids={"99"})
        self._admin_callback(bot, tg, "admin:result:M01")
        self._admin_callback(bot, tg, "admin:home-goals:M01:5p")
        self._admin_callback(bot, tg, "admin:away-goals:M01:5p")
        self.assertIn("Укажите исход", tg.edits[-1][2])
        self._admin_callback(bot, tg, "admin:score-outcome:M01:Х")
        result = repo.results("R1")[0]
        market = __import__("tak_ili_inache.models", fromlist=["Market"]).Market
        self.assertEqual(result.winning_markets, frozenset({market.X, market.ONE_X, market.X_TWO, market.TB}))

        self._admin_callback(bot, tg, "admin:result:M02")
        self._admin_callback(bot, tg, "admin:return:M02")
        returned = next(item for item in repo.results("R1") if item.match_id == "M02")
        self.assertEqual(returned.returned_markets, frozenset(market))

    def test_score_publishes_full_leaderboard_not_only_first_five(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(repo, tg, lambda: self.round_.deadline_msk, admin_ids={"99"}, tournament_chat_id=-100, output_dir=output)
            repo.save_round(self.round_)
            base = __import__("test_validators").valid_prediction()
            for index in range(6):
                participant = repo.register_participant(str(index), f"Игрок {index}")
                repo.save_prediction(base.__class__("R1", participant.participant_id, base.bets, base.submitted_at_msk), f"p{index}")
            for fixture in self.round_.fixtures:
                repo.save_result(__import__("tak_ili_inache.models", fromlist=["BetResult"]).BetResult(fixture.match_id, frozenset({__import__("tak_ili_inache.models", fromlist=["Market"]).Market.P1, __import__("tak_ili_inache.models", fromlist=["Market"]).Market.ONE_X, __import__("tak_ili_inache.models", fromlist=["Market"]).Market.TB})))
            self._admin_callback(bot, tg, "admin:score")
            self.assertIn("сверены", tg.messages[-1][1])
            self.assertFalse(any(chat_id == -100 for chat_id, _, _ in tg.messages))
            self.assertEqual(len([item for item in tg.documents if item[0] == -100]), 1)
            self.assertEqual(len([item for item in tg.photos if item[0] == -100]), 2)
            self.assertTrue(any("Итоговый рейтинг" in item[2] for item in tg.photos))
            self.assertEqual(sum(item[2] == "" for item in tg.photos if item[0] == -100), 1)

    def test_final_group_leaderboard_is_public_formula_safe_and_idempotent(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(repo, tg, lambda: self.round_.deadline_msk, admin_ids={"99"}, tournament_chat_id=-100, output_dir=output)
            repo.save_round(self.round_)
            base = __import__("test_validators").valid_prediction()
            names = ('=HYPERLINK("https://invalid")', "+cmd", "\tcmd", "Одинаковый", "Одинаковый")
            for index, name in enumerate(names):
                participant = repo.register_participant(str(123456 + index), name)
                repo.save_prediction(base.__class__("R1", participant.participant_id, base.bets, base.submitted_at_msk), f"p{index}")
            market = __import__("tak_ili_inache.models", fromlist=["Market"]).Market
            result_type = __import__("tak_ili_inache.models", fromlist=["BetResult"]).BetResult
            for fixture in self.round_.fixtures:
                repo.save_result(result_type(fixture.match_id, frozenset({market.P1, market.ONE_X, market.TB})))

            bot._score(9, "99")
            group_documents = [item for item in tg.documents if item[0] == -100]
            group_photos = [item for item in tg.photos if item[0] == -100]
            self.assertEqual(len(group_documents), 1)
            self.assertEqual(len(group_photos), 2)
            path = Path(group_documents[0][1])
            self.assertEqual(path.name, "final_leaderboard.csv")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"tg:123456", raw)
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(
                list(rows[0]),
                ["round_id", "player_no", "rank", "display_name", "gross_payout", "net_result", "winning_bets", "single_payout", "express_payout"],
            )
            self.assertFalse({"participant_id", "telegram_id", "match_id", "submitted_at"} & set(rows[0]))
            self.assertIn("'=HYPERLINK(\"https://invalid\")", {row["display_name"] for row in rows})
            self.assertIn("'+cmd", {row["display_name"] for row in rows})
            self.assertIn("'\tcmd", {row["display_name"] for row in rows})
            duplicates = [row for row in rows if row["display_name"] == "Одинаковый"]
            self.assertEqual(len(duplicates), 2)
            self.assertEqual(len({row["player_no"] for row in duplicates}), 2)

            bot._score(9, "99")
            self.assertEqual(len([item for item in tg.documents if item[0] == -100]), 1)
            self.assertEqual(len([item for item in tg.photos if item[0] == -100]), 2)

    def test_outbox_pending_step_is_not_resent_after_persistence_failure(self) -> None:
        class FailingRepository(FakeRepository):
            def __init__(self):
                super().__init__()
                self.fail_once = True

            def mark_operation_done(self, operation_key):
                if self.fail_once:
                    self.fail_once = False
                    raise OSError("simulated disk failure after Telegram accepted send")
                super().mark_operation_done(operation_key)

        with tempfile.TemporaryDirectory() as output:
            repo, tg = FailingRepository(), FakeTelegram()
            repo.save_round(self.round_)
            participant = repo.register_participant("42", "Игрок")
            base = __import__("test_validators").valid_prediction()
            repo.save_prediction(base.__class__("R1", participant.participant_id, base.bets, base.submitted_at_msk))
            bot = BotService(repo, tg, lambda: self.round_.deadline_msk, admin_ids={"99"}, tournament_chat_id=-100, output_dir=output)
            bot._current_update_id = "first"
            with self.assertRaises(OSError):
                bot._publish(9, "99")
            first_count = len([item for item in tg.documents if item[0] == -100]) + len([item for item in tg.photos if item[0] == -100])
            bot._current_update_id = "replay"
            bot._publish(9, "99")
            self.assertEqual(
                len([item for item in tg.documents if item[0] == -100]) + len([item for item in tg.photos if item[0] == -100]),
                first_count,
            )
            self.assertIn("Восстановить отправки", tg.messages[-1][1])
            bot._outbox_menu(9, "99")
            retry_button = next(row[0]["callback_data"] for row in tg.messages[-1][2]["inline_keyboard"] if row[0]["callback_data"].startswith("admin:outbox-retry:"))
            bot._handle_callback({"id": "recover", "from": {"id": 99}, "data": retry_button, "message": {"message_id": 1, "chat": {"id": 9, "type": "private"}}})
            bot._publish(9, "99")
            self.assertGreater(
                len([item for item in tg.documents if item[0] == -100]) + len([item for item in tg.photos if item[0] == -100]),
                first_count,
            )

    def _event(self, match_id: str) -> None:
        self._callback(f"match:{match_id}")
        self._callback(f"market:{match_id}:П1")

    def _message(self, text: str) -> None:
        self.bot.handle_update({"message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 42, "first_name": "Тестер"}, "text": text}})

    def _callback(self, data: str) -> None:
        self.bot.handle_update({"callback_query": {"id": f"q{len(self.tg.answered)}", "from": {"id": 42}, "data": data, "message": {"message_id": len(self.tg.cleared) + 1, "chat": {"id": 1, "type": "private"}}}})

    @staticmethod
    def _admin_callback(bot, tg, data: str) -> None:
        bot.handle_update({"update_id": len(tg.answered) + 100, "callback_query": {"id": f"a{len(tg.answered)}", "from": {"id": 99}, "data": data, "message": {"message_id": len(tg.cleared) + 1, "chat": {"id": 9, "type": "private"}}}})


def _labels(keyboard):
    return [row[0]["text"] for row in keyboard["inline_keyboard"]]
