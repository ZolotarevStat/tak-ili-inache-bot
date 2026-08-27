from __future__ import annotations

import csv
import re
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from tak_ili_inache.bot import BotService, _interim_caption, _top_stats_caption
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Bet, BetEvent, BetResult, BetType, Fixture, Market, Participant, Prediction, Round
from tak_ili_inache.presentation import compact_match_label, compact_team_name
from tak_ili_inache.reporting import (
    HEATMAP_GRID_TOP,
    HEATMAP_HEADER_Y,
    HEATMAP_LABEL_GAP,
    HEATMAP_LABEL_WIDTH,
    HEATMAP_LEFT,
    HEATMAP_CELL_WIDTH,
    HEATMAP_ROW_HEIGHT,
    HEATMAP_SUBTITLE_Y,
    OUTCOME_HEATMAP_GRID_TOP,
    OUTCOME_HEATMAP_HEADER_Y,
    OUTCOME_HEATMAP_LEGEND_Y,
    OUTCOME_LEGEND,
    PENDING_OUTCOME_COLOR,
    _fonts,
    _fixture_label,
    _heatmap_layout,
    _outcome_cell_color,
    build_interim_results_export,
    build_outcome_chart,
    build_popularity_chart,
    build_predictions_export,
    build_public_coupons_csv,
    build_reports,
)

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
            with path.open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 6)
            self.assertEqual(rows[0]["display_name"], "Антон Тестовый")
            self.assertEqual(rows[0]["home_team"], "Мексика")
            self.assertEqual(rows[0]["away_team"], "ЮАР")
            self.assertNotIn("telegram_id", rows[0])
            self.assertNotIn("participant_id", rows[0])
            self.assertNotIn("777777", path.read_text(encoding="utf-8-sig"))

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

    def test_selection_heatmap_keeps_subtitle_headers_and_grid_separate(self) -> None:
        title_font, bold_font, regular_font = _fonts()
        subtitle_box = regular_font.getbbox("Тур PILOT-20260826: число выборов по каждому матчу")
        header_box = bold_font.getbbox("П1")
        subtitle_bottom = HEATMAP_SUBTITLE_Y + subtitle_box[3]
        header_top = HEATMAP_HEADER_Y + header_box[1]
        header_bottom = HEATMAP_HEADER_Y + header_box[3]
        self.assertLess(subtitle_bottom, header_top)
        self.assertLess(header_bottom, HEATMAP_GRID_TOP)
        self.assertNotEqual(bold_font.path, regular_font.path)
        self.assertNotEqual(bytes(bold_font.getmask("Игрок")), bytes(regular_font.getmask("Игрок")))
        self.assertGreater(title_font.size, bold_font.size)

        with tempfile.TemporaryDirectory() as output:
            prediction = self._prediction("player", (Market.P1,) * 6)
            path, _ = build_popularity_chart(output, self.round_, (prediction,))
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            with Image.open(path) as image:
                self.assertLessEqual(image.width, 900)
                self.assertLessEqual(image.height, 900)

    def test_long_fixture_labels_stay_in_shared_label_zone_before_every_grid(self) -> None:
        names = (
            ("АЕК Афины", "Левски София"),
            ("Виктория Пльзень", "Црвена Звезда"),
            ("Жальгирис Каунас", "Бешикташ"),
        )
        fixtures = tuple(
            Fixture(
                fixture.round_id,
                fixture.match_id,
                fixture.kickoff_msk,
                *(names[index] if index < len(names) else (fixture.home_team, fixture.away_team)),
                fixture.total_line,
                fixture.odds,
            )
            for index, fixture in enumerate(self.round_.fixtures)
        )
        long_round = Round(self.round_.round_id, fixtures, self.round_.deadline_msk, "long-labels")
        _, bold_font, _ = _fonts()
        selection_layout = _heatmap_layout(HEATMAP_GRID_TOP, len(fixtures), 40)
        outcome_layout = _heatmap_layout(OUTCOME_HEATMAP_GRID_TOP, len(fixtures), 54)
        self.assertEqual(selection_layout.grid_x, HEATMAP_LEFT)
        self.assertEqual(selection_layout.label_x + HEATMAP_LABEL_WIDTH + HEATMAP_LABEL_GAP, selection_layout.grid_x)
        for layout in (selection_layout, outcome_layout):
            canvas = Image.new("RGB", (layout.width, layout.height), "white")
            draw = ImageDraw.Draw(canvas)
            for row_no, fixture in enumerate(fixtures):
                label = _fixture_label(fixture, bold_font, layout.label_width)
                self.assertNotIn("\n", label)
                bbox = draw.textbbox((layout.label_x, layout.grid_top + row_no * layout.row_height + 14), label, font=bold_font)
                self.assertLessEqual(bbox[2], layout.grid_x - layout.label_gap)
                self.assertLessEqual(bbox[2], layout.label_x + layout.label_width)

        prediction = self._prediction("player", (Market.P1,) * 6)
        results = (BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})),)
        with tempfile.TemporaryDirectory() as output:
            selection, _ = build_popularity_chart(output, long_round, (prediction,))
            outcome = build_outcome_chart(output, long_round, (prediction,), results)
            for path in (selection, outcome):
                with Image.open(path) as image:
                    self.assertLessEqual(image.width, 900)
                    self.assertLessEqual(image.height, 900)

    def test_outcome_heatmap_uses_settled_and_pending_states(self) -> None:
        prediction = self._prediction(
            "player", (Market.P1, Market.P2, Market.TB, Market.TM, Market.ONE_X, Market.X_TWO)
        )
        results = (
            BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})),
            BetResult("M02", frozenset({Market.P1, Market.ONE_X, Market.TB})),
            BetResult("M03", frozenset(), frozenset(Market)),
        )
        with tempfile.TemporaryDirectory() as output:
            interim = build_outcome_chart(output, self.round_, (prediction,), results)
            self.assertEqual(interim.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            with Image.open(interim) as image:
                image.verify()
            self.assertEqual(_outcome_cell_color("pending", 0), PENDING_OUTCOME_COLOR)
            self.assertNotEqual(_outcome_cell_color("won", 0), PENDING_OUTCOME_COLOR)
            self.assertNotEqual(_outcome_cell_color("lost", 0), PENDING_OUTCOME_COLOR)
            self.assertNotEqual(_outcome_cell_color("returned", 0), PENDING_OUTCOME_COLOR)
            self.assertEqual({item[1] for item in OUTCOME_LEGEND}, {"won", "lost", "returned", "pending"})
            _, bold_font, regular_font = _fonts()
            self.assertLess(HEATMAP_SUBTITLE_Y + regular_font.getbbox("Тур R1: число выборов; цвет показывает исход события")[3], OUTCOME_HEATMAP_LEGEND_Y)
            self.assertLess(OUTCOME_HEATMAP_LEGEND_Y + regular_font.getbbox("ожидается")[3], OUTCOME_HEATMAP_HEADER_Y)
            self.assertLess(OUTCOME_HEATMAP_HEADER_Y + bold_font.getbbox("П1")[3], OUTCOME_HEATMAP_GRID_TOP)
            with Image.open(interim) as image:
                # M01/P1: settled and selected; M01/P2: settled but unselected;
                # M03/P1: returned but unselected; M04/P1: genuinely pending.
                cell = lambda row, column: image.getpixel((
                    HEATMAP_LEFT + column * HEATMAP_CELL_WIDTH + 12,
                    OUTCOME_HEATMAP_GRID_TOP + row * HEATMAP_ROW_HEIGHT + 12,
                ))
                self.assertEqual(cell(0, 0), _outcome_cell_color("won", 1))
                self.assertEqual(cell(0, 2), _outcome_cell_color("lost", 0))
                self.assertEqual(cell(2, 0), _outcome_cell_color("returned", 0))
                self.assertEqual(cell(3, 0), PENDING_OUTCOME_COLOR)
            with self.assertRaisesRegex(ValueError, "every match result"):
                build_outcome_chart(output, self.round_, (prediction,), results, final=True)

    def test_final_scoring_rejects_pending_results_without_group_publication(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        participant = repo.register_participant("1", "Игрок")
        repo.save_prediction(self._prediction(participant.participant_id, (Market.P1,) * 6), "p1")
        repo.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})))
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(repo, tg, lambda: self.round_.deadline_msk, admin_ids={"99"}, tournament_chat_id=-100, output_dir=output)
            bot._score(9, "99")
        self.assertIn("результаты не для всех матчей", tg.messages[-1][1])
        self.assertFalse(any(item[0] == -100 for item in tg.documents + tg.photos))

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
            self.assertEqual(len([item for item in tg.photos if item[0] == -100]), 1)
            self.assertEqual(tg.photos[-1][2], "")
            self.assertEqual(tg.document_parse_modes[-1], "HTML")
            self.assertIn("Матчей: 1/12", group[0][2])
            self.assertIn("Игрок Один", group[0][2])
            self.assertIn("Рейтинг предварительный", group[0][2])
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
            self.assertEqual(len([item for item in tg.photos if item[0] == -100]), 1)

            restarted = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            restarted._publish_interim(9, "99")
            self.assertEqual(len([item for item in tg.documents if item[0] == -100]), 1)
            self.assertEqual(len([item for item in tg.photos if item[0] == -100]), 1)

            repo.save_result(BetResult("M02", frozenset({Market.P1, Market.ONE_X, Market.TB})))
            bot._publish_interim(9, "99")
            self.assertEqual(len([item for item in tg.documents if item[0] == -100]), 2)
            self.assertEqual(len([item for item in tg.photos if item[0] == -100]), 2)

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

    def test_interim_caption_uses_compact_ranked_payouts_and_one_footer(self) -> None:
        caption = _interim_caption(
            "R1",
            1,
            12,
            [SimpleNamespace(rank=1, participant_id="player", realized_payout=1570, maximum_payout=8783)],
            {"player": "Андрей Селяев"},
        )
        ranking_lines = [line for line in caption.splitlines() if re.match(r"^\d+\. ", line)]
        self.assertEqual(ranking_lines, ["1. <b>Андрей Селяев</b> — 1.570 (8.783)"])
        self.assertFalse(any(word in ranking_lines[0] for word in ("начислено", "ещё возможно", "максимум")))
        self.assertIn("В скобках — итог, если зайдут все оставшиеся ставки.", caption)
        self.assertIn("Рейтинг предварительный: экспрессы", caption)

    def test_interim_caption_escapes_name_and_keeps_markup_complete_under_limit(self) -> None:
        caption = _interim_caption(
            "R1",
            1,
            12,
            [SimpleNamespace(rank=1, participant_id="player", realized_payout=1570, maximum_payout=8783)],
            {"player": '<Андрей & "ссылка">'},
        )
        self.assertIn("1. <b>&lt;Андрей &amp; &quot;ссылка&quot;&gt;</b> — 1.570 (8.783)", caption)
        self.assertNotIn('<Андрей', caption)
        self.assertLessEqual(len(caption), 1000)
        self.assertEqual(caption.count("<b>"), caption.count("</b>"))

    def test_legacy_interim_v4_pending_blocks_html_caption_publication(self) -> None:
        repo, tg = FakeRepository(), FakeTelegram()
        repo.save_round(self.round_)
        participant = repo.register_participant("1", "Игрок")
        repo.save_prediction(self._prediction(participant.participant_id, (Market.P1,) * 6), "p1")
        repo.save_result(BetResult("M01", frozenset({Market.P1, Market.ONE_X, Market.TB})))
        repo.begin_operation("interim-v4:R1:legacy:group-document")
        with tempfile.TemporaryDirectory() as output:
            bot = BotService(
                repo, tg, lambda: self.round_.deadline_msk,
                admin_ids={"99"}, tournament_chat_id=-100, output_dir=output,
            )
            bot._publish_interim(9, "99")
        self.assertFalse(any(item[0] == -100 for item in tg.documents + tg.photos))
        self.assertIn("Восстановить отправки", tg.messages[-1][1])

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

    def test_excel_safe_decimal_exports_use_comma_bom_and_preserve_exact_odds(self) -> None:
        """CSV odds are quoted text values, never Russian-Excel date-like ``m.d``."""
        odds = (Decimal("1.14"), Decimal("1.54"), Decimal("2.02"), Decimal("2.14"), Decimal("3.60"), Decimal("1.14"))
        events = tuple(
            BetEvent(
                fixture.match_id,
                Market.TB if index == 4 else Market.P1,
                value,
                Decimal("2.5") if index == 4 else None,
            )
            for index, (fixture, value) in enumerate(zip(self.round_.fixtures[:6], odds))
        )
        prediction = Prediction(
            "R1",
            "player",
            (
                Bet(BetType.SINGLE, 1000, (events[0],)),
                Bet(BetType.SINGLE, 1000, (events[1],)),
                Bet(BetType.SINGLE, 1000, (events[2],)),
                Bet(BetType.SINGLE, 500, (events[3],)),
                Bet(BetType.EXPRESS, 1500, (events[4], events[5])),
            ),
            self.round_.deadline_msk - timedelta(minutes=1),
        )
        participant = Participant("player", "private-id", "=Формула")
        results = tuple(BetResult(fixture.match_id, frozenset({Market.P1})) for fixture in self.round_.fixtures)
        expected = {"1,14", "1,54", "2,02", "2,14", "3,60"}
        prohibited = {"1.14", "1.54", "2.02", "2.14", "3.60"}
        with tempfile.TemporaryDirectory() as output:
            public = build_public_coupons_csv(output, self.round_, (prediction,), (participant,))
            interim, _ = build_interim_results_export(output, self.round_, (prediction,), results[:1], (participant,))
            admin = build_predictions_export(output, self.round_, (prediction,), (participant,))
            final = build_reports(output, "+R1", (prediction,), results, (participant,), prediction.submitted_at_msk)["scoring"]
            for path in (public, interim, admin, final):
                raw = path.read_bytes()
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), path.name)
                decoded = raw.decode("utf-8-sig")
                self.assertFalse(any(value in decoded for value in prohibited), path.name)
                with path.open(encoding="utf-8-sig", newline="") as source:
                    rows = list(csv.DictReader(source))
                self.assertTrue(rows, path.name)

            with public.open(encoding="utf-8-sig", newline="") as source:
                public_rows = list(csv.DictReader(source))
            event_odds = {row["Коэф. события"] for row in public_rows}
            self.assertTrue(expected.issubset(event_odds))
            self.assertEqual(
                {Decimal(value.replace(",", ".")) for value in event_odds},
                {Decimal(value) for value in odds},
            )
            self.assertIn("ТБ 2,5", {row["Исход"] for row in public_rows})
            self.assertEqual(public_rows[0]["Игрок"], "'=Формула")

            with admin.open(encoding="utf-8-sig", newline="") as source:
                admin_rows = list(csv.DictReader(source))
            self.assertTrue(expected.issubset({row["odds_snapshot"] for row in admin_rows}))
            self.assertEqual("2,5", next(row["total_line"] for row in admin_rows if row["market"] == "ТБ"))

            with final.open(encoding="utf-8-sig", newline="") as source:
                final_rows = list(csv.DictReader(source))
            self.assertTrue(all("," in row["combined_odds"] for row in final_rows))
            self.assertEqual(final_rows[0]["round_id"], "'+R1")

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
