from __future__ import annotations

import csv
import re
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tak_ili_inache.bot import BotService
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Bet, BetEvent, BetType, Fixture, Market, Prediction
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

    def test_publication_uses_match_names_top_10_and_full_png(self) -> None:
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
            group_texts = [text for chat_id, text, _ in tg.messages if chat_id == -100]
            coupons = [text for text in group_texts if text.startswith("Купон:")]
            self.assertEqual(len(coupons), 2)
            self.assertTrue(all(not re.search(r"\bM\d{2}\b", text) for text in coupons))
            self.assertTrue(any("Мексика — ЮАР · П1" in text for text in coupons))
            stats = next(text for text in group_texts if text.startswith("Топ-10"))
            self.assertEqual(len(re.findall(r"(?m)^\d+\. ", stats)), 10)
            self.assertNotRegex(stats, r"\bM\d{2}\b")
            self.assertEqual(len(tg.photos), 1)
            chart = Path(tg.photos[0][1])
            self.assertEqual(chart.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(tg.photos[0][2], "Полная статистика выбора событий")

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
