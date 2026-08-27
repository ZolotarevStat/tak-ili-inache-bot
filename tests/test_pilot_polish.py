from __future__ import annotations

import csv
import re
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tak_ili_inache.bot import BotService, _top_stats_caption
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Bet, BetEvent, BetResult, BetType, Fixture, Market, Prediction
from tak_ili_inache.presentation import compact_match_label, compact_team_name

from tests.test_bot_flow import FakeTelegram


class PilotPolishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.round_ = import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")

    def test_mobile_match_labels_are_one_column_and_deterministically_shortened(self) -> None:
        fixture = Fixture(
            "R", "LONG", self.round_.fixtures[0].kickoff_msk,
            "Хапоэль Беер-Шева", "Очень Длинное Название Клуба", Decimal("2.5"), self.round_.fixtures[0].odds,
        )
        self.assertEqual(compact_team_name("Хапоэль Беер-Шева"), "Хапоэль БШ")
        self.assertLessEqual(len(compact_team_name("Очень Длинное Название Клуба")), 14)
        self.assertIn("26.08" if fixture.kickoff_msk.month == 8 else fixture.kickoff_msk.strftime("%d.%m"), compact_match_label(fixture))

        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        repo.register_participant("42", "Игрок")
        bot = BotService(repo, tg, lambda: self.round_.deadline_msk - timedelta(minutes=10))
        bot._begin_prediction(1, "42")
        rows = tg.messages[-1][2]["inline_keyboard"]
        match_rows = [row for row in rows if row[0]["callback_data"].endswith(tuple(f"match:M{i:02d}" for i in range(1, 13)))]
        self.assertEqual(len(match_rows), 6)
        self.assertTrue(all(len(row) == 1 for row in match_rows))

    def test_admin_export_is_human_readable_long_csv_without_telegram_id(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        participant = repo.register_participant("777777", "Антон Тестовый")
        prediction = self._prediction(participant.participant_id, (Market.P1,) * 6)
        repo.save_prediction(prediction)
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(repo, tg, lambda: self.round_.deadline_msk, admin_ids={"99"}, output_dir=output)
            bot._admin_menu(9, "99")
            labels = [button["text"] for row in tg.messages[-1][2]["inline_keyboard"] for button in row]
            self.assertIn("Выгрузить прогнозы CSV", labels)
            bot._export_predictions(9, "99")
            path = Path(tg.documents[-1][1])
            self.assertTrue(path.is_file())
            with path.open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 6)
            self.assertEqual(rows[0]["display_name"], "Антон Тестовый")
            self.assertEqual(rows[0]["home_team"], "Мексика")
            self.assertEqual(rows[0]["away_team"], "ЮАР")
            self.assertNotIn("telegram_id", rows[0])
            self.assertNotIn("participant_id", rows[0])
            self.assertNotIn("777777", path.read_text(encoding="utf-8"))

    def test_publication_uses_one_coupon_csv_and_one_top_10_png(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        first = repo.register_participant("1", "Игрок Один")
        second = repo.register_participant("2", "Игрок Два")
        repo.save_prediction(self._prediction(first.participant_id, (Market.P1,) * 6), "p1")
        repo.save_prediction(
            self._prediction(second.participant_id, (Market.X, Market.P2, Market.TB, Market.TM, Market.ONE_X, Market.X_TWO)),
            "p2",
        )
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            bot._publish(9, "99")
            self.assertFalse(any(chat_id == -100 for chat_id, _, _ in tg.messages))
            group_documents = [item for item in tg.documents if item[0] == -100]
            self.assertEqual(len(group_documents), 1)
            path = Path(group_documents[0][1])
            self.assertEqual(path.suffix, ".csv")
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual({row["Игрок"] for row in rows}, {"Игрок Один", "Игрок Два"})
            self.assertTrue(any(row["Матч"] == "Мексика — ЮАР" and row["Исход"] == "П1" for row in rows))
            coupons = path.read_text(encoding="utf-8-sig")
            self.assertNotRegex(coupons, r"\bM\d{2}\b")
            self.assertNotIn("participant_id", coupons)
            self.assertNotIn("telegram_id", coupons)
            self.assertEqual(len(tg.photos), 1)
            chart = Path(tg.photos[0][1])
            self.assertEqual(chart.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            caption = tg.photos[0][2]
            self.assertEqual(len(re.findall(r"(?m)^\d+\. ", caption)), 10)
            self.assertNotRegex(caption, r"\bM\d{2}\b")

    def test_compact_publication_requires_reconciliation_of_legacy_pending_send(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        participant = repo.register_participant("1", "Игрок Один")
        repo.save_prediction(self._prediction(participant.participant_id, (Market.P1,) * 6), "p1")
        repo.begin_operation("publish-v2:R1:legacy:coupon:1")
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            bot._publish(9, "99")
        self.assertFalse(any(item[0] == -100 for item in tg.documents + tg.photos))
        self.assertIn("Восстановить отправки", tg.messages[-1][1])

    def test_interim_results_are_one_idempotent_csv_document(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        first = repo.register_participant("1", "Игрок Один")
        second = repo.register_participant("2", "Игрок Два")
        repo.save_prediction(self._prediction(first.participant_id, (Market.P1,) * 6), "p1")
        repo.save_prediction(self._prediction(second.participant_id, (Market.P2,) * 6), "p2")
        repo.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})))
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            bot._admin_menu(9, "99")
            labels = [button["text"] for row in tg.messages[-1][2]["inline_keyboard"] for button in row]
            self.assertIn("Опубликовать промежуточный рейтинг", labels)

            bot._publish_interim(9, "99")
            group = [item for item in tg.documents if item[0] == -100]
            self.assertEqual(len(group), 1)
            self.assertFalse(any(chat_id == -100 for chat_id, _, _ in tg.messages))
            self.assertIn("Матчей: 1/12", group[0][2])
            self.assertIn("Игрок Один", group[0][2])
            self.assertIn("Максимум не гарантирован", group[0][2])
            self.assertLessEqual(len(group[0][2]), 1000)
            with Path(group[0][1]).open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            first_rows = [row for row in rows if row["Игрок"] == "Игрок Один"]
            self.assertEqual(first_rows[0]["Статус события"], "зашло")
            self.assertEqual(first_rows[0]["Статус ставки"], "зашло")
            self.assertGreater(int(first_rows[0]["Начислено"]), 0)
            self.assertGreater(int(first_rows[0]["Макс. выплата ожидающих"]), 0)
            self.assertEqual(
                int(first_rows[0]["Макс. итог"]),
                int(first_rows[0]["Начислено"]) + int(first_rows[0]["Макс. выплата ожидающих"]),
            )
            bot._publish_interim(9, "99")
            self.assertEqual(len([item for item in tg.documents if item[0] == -100]), 1)

            repo.save_result(BetResult("M02", frozenset({Market.P1, Market.ONE_X, Market.TB})))
            bot._publish_interim(9, "99")
            self.assertEqual(len([item for item in tg.documents if item[0] == -100]), 2)

    def test_interim_caption_stays_within_telegram_document_limit(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        for index in range(40):
            participant = repo.register_participant(str(index), f"Игрок {index} " + "ОченьДлинноеИмя" * 7)
            repo.save_prediction(self._prediction(participant.participant_id, (Market.P1,) * 6), f"p{index}")
        repo.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})))
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            bot._publish_interim(9, "99")
            caption = next(caption for chat_id, _, caption in tg.documents if chat_id == -100)
            self.assertLessEqual(len(caption), 1000)
            self.assertIn("…ещё участников:", caption)

    def test_group_csv_neutralizes_formula_prefixes_and_newlines(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        participant = repo.register_participant("1", "=SUM(A1:A2)\nИгрок")
        repo.save_prediction(self._prediction(participant.participant_id, (Market.P1,) * 6), "p1")
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            bot._publish(9, "99")
            with Path(tg.documents[0][1]).open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
        self.assertEqual(rows[0]["Игрок"], "'=SUM(A1:A2) Игрок")

    def test_successful_admin_publication_refreshes_card_without_private_log_message(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        participant = repo.register_participant("1", "Игрок")
        repo.save_prediction(self._prediction(participant.participant_id, (Market.P1,) * 6), "p1")
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            bot._publish(9, "99", message_id=123)
        self.assertEqual(tg.send_calls, 0)
        self.assertEqual(len(tg.edits), 1)
        self.assertEqual(tg.edits[0][:2], (9, 123))
        self.assertIn("🛠️ Админские команды", tg.edits[0][2])

    def test_top_10_caption_stays_below_telegram_photo_limit(self) -> None:
        rows = [
            {"label": "Очень длинная команда " * 30, "count": index}
            for index in range(1, 11)
        ]
        caption = _top_stats_caption("ROUND-" + "X" * 100, rows)
        self.assertLessEqual(len(caption), 1000)
        self.assertIn("Полная статистика", caption)

    def _prediction(self, participant_id: str, markets: tuple[Market, ...]) -> Prediction:
        events = []
        for fixture, market in zip(self.round_.fixtures[:6], markets):
            events.append(
                BetEvent(
                    fixture.match_id,
                    market,
                    fixture.odds[market],
                    fixture.total_line if market in {Market.TB, Market.TM} else None,
                )
            )
        bets = (
            Bet(BetType.SINGLE, 1000, (events[0],)),
            Bet(BetType.SINGLE, 1000, (events[1],)),
            Bet(BetType.SINGLE, 1000, (events[2],)),
            Bet(BetType.SINGLE, 500, (events[3],)),
            Bet(BetType.EXPRESS, 1500, (events[4], events[5])),
        )
        return Prediction("R1", participant_id, bets, self.round_.deadline_msk - timedelta(minutes=1))
