#!/usr/bin/env python3
"""Local-only prototype for per-player Telegram statistics cards.

The script uses synthetic predictions and never talks to Telegram or production.
PNG files are written only because this is an explicit visual prototype; the
bot imports the shared renderer and keeps production PNGs in memory.
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.models import Bet, BetEvent, BetResult, BetType, Market, Participant, Prediction, Round
from tak_ili_inache.player_cards import (
    CARD_HEIGHT,
    CARD_WIDTH,
    build_player_cards,
    card_bet_boxes,
    media_batches,
    nearest_neighbours,
    prediction_similarity,
    similarity_order,
)
from tak_ili_inache.reporting import RenderedPng
SYNTHETIC_NAMES = (
    "Алексей", "Мария", "Илья", "Софья", "Денис",
    "Анна", "Максим", "Полина", "Роман", "Дарья",
    "Никита", "Елена", "Артём", "Ольга", "Кирилл",
    "Вера", "Павел", "Алина", "Сергей", "Людмила",
    "Глеб", "Екатерина", "Михаил", "Ирина", "Лев",
)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _fonts() -> dict[str, ImageFont.FreeTypeFont]:
    families = (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    )
    for bold_path, regular_path in families:
        if Path(bold_path).is_file() and Path(regular_path).is_file():
            return {
                "title": ImageFont.truetype(bold_path, 52),
                "summary": ImageFont.truetype(bold_path, 28),
                "bet": ImageFont.truetype(bold_path, 24),
                "body_bold": ImageFont.truetype(bold_path, 21),
                "body": ImageFont.truetype(regular_path, 22),
                "small_bold": ImageFont.truetype(bold_path, 18),
                "small": ImageFont.truetype(regular_path, 19),
                "tiny_bold": ImageFont.truetype(bold_path, 14),
                "footer": ImageFont.truetype(bold_path, 24),
            }
    raise RuntimeError("Unicode font not found")


def synthetic_data(round_: Round, player_count: int) -> tuple[tuple[Prediction, ...], tuple[BetResult, ...], tuple[Participant, ...]]:
    if player_count < 1 or player_count > len(SYNTHETIC_NAMES):
        raise ValueError(f"player_count must be between 1 and {len(SYNTHETIC_NAMES)}")
    market_cycles = (
        (Market.P1, Market.X, Market.P2, Market.TB, Market.ONE_X, Market.TM, Market.X_TWO),
        (Market.P1, Market.X, Market.P2, Market.TM, Market.ONE_X, Market.TB, Market.X_TWO),
        (Market.ONE_X, Market.X, Market.X_TWO, Market.TB, Market.P1, Market.TM, Market.P2),
        (Market.P2, Market.X_TWO, Market.P1, Market.TM, Market.X, Market.TB, Market.ONE_X),
    )
    clusters = (
        (0, 1, 2, 3, 4, 5, 6),
        (2, 3, 4, 5, 6, 7, 8),
        (5, 6, 7, 8, 9, 10, 11),
        (0, 2, 4, 6, 8, 10, 11),
    )
    predictions = []
    participants = []
    for player_no in range(player_count):
        participant_id = f"synthetic-{player_no + 1:02}"
        participants.append(Participant(participant_id, str(900000 + player_no), SYNTHETIC_NAMES[player_no]))
        cluster_no = min(player_no // 5, len(clusters) - 1)
        indexes = clusters[cluster_no]
        market_pattern = market_cycles[(player_no % 5) // 2 if player_no % 5 < 4 else 3]
        events = []
        for position, fixture_index in enumerate(indexes):
            fixture = round_.fixtures[fixture_index]
            market = market_pattern[position]
            events.append(BetEvent(
                fixture.match_id,
                market,
                fixture.odds[market],
                fixture.total_line if market in {Market.TB, Market.TM} else None,
            ))
        bets = (
            Bet(BetType.SINGLE, 900, (events[0],)),
            Bet(BetType.SINGLE, 900, (events[1],)),
            Bet(BetType.SINGLE, 900, (events[2],)),
            Bet(BetType.SINGLE, 800, (events[3],)),
            Bet(BetType.EXPRESS, 1500, tuple(events[4:])),
        )
        predictions.append(Prediction(round_.round_id, participant_id, bets, round_.deadline_msk - timedelta(minutes=player_no + 1)))
    result_patterns = (
        frozenset({Market.P1, Market.ONE_X, Market.TB}),
        frozenset({Market.X, Market.ONE_X, Market.X_TWO, Market.TM}),
        frozenset({Market.P2, Market.X_TWO, Market.TB}),
    )
    results = tuple(
        BetResult(fixture.match_id, result_patterns[index % len(result_patterns)], score_label=("2:1", "1:1", "0:2")[index % 3])
        for index, fixture in enumerate(round_.fixtures[:8])
    )
    return tuple(predictions), results, tuple(participants)


def _contact_sheet(cards: Sequence[RenderedPng], batch_no: int) -> bytes:
    columns, thumb_width = 2, 480
    thumb_height = int(CARD_HEIGHT * thumb_width / CARD_WIDTH)
    rows = (len(cards) + columns - 1) // columns
    canvas = Image.new("RGB", (columns * thumb_width + 60, rows * thumb_height + 100), "#0B0F17")
    draw = ImageDraw.Draw(canvas)
    title_font = _fonts()["bet"]
    draw.text((30, 24), f"Синтетический Telegram-батч {batch_no} · {len(cards)} карточек", font=title_font, fill="#F7F9FC")
    for index, card in enumerate(cards):
        with Image.open(BytesIO(card.content)) as source:
            thumbnail = source.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        x = 30 + (index % columns) * thumb_width
        y = 80 + (index // columns) * thumb_height
        canvas.paste(thumbnail, (x, y))
    return _png_bytes(canvas)


def write_demo(output_dir: Path, player_count: int, fixture_path: Path) -> dict:
    round_ = import_fixtures(fixture_path)
    predictions, results, participants = synthetic_data(round_, player_count)
    cards = build_player_cards(round_, predictions, results, participants)
    batches = media_batches(cards)
    output_dir.mkdir(parents=True, exist_ok=True)
    names_by_id = {item.participant_id: item.display_name for item in participants}
    ordered = similarity_order(predictions, names_by_id)
    neighbours = nearest_neighbours(predictions, names_by_id)
    for card in cards:
        (output_dir / card.filename).write_bytes(card.content)
    for batch_no, batch in enumerate(batches, 1):
        (output_dir / f"batch-{batch_no:02}-preview.png").write_bytes(_contact_sheet(batch, batch_no))
    manifest = {
        "synthetic": True,
        "player_count": player_count,
        "batch_sizes": [len(batch) for batch in batches],
        "similarity_formula": "0.70 * match_jaccard + 0.30 * exact_outcome_over_match_union",
        "order": [
            {
                "card": index,
                "player": names_by_id[item.participant_id],
                "nearest": names_by_id.get(neighbours[item.participant_id].participant_id or "", "—"),
                "score": str(neighbours[item.participant_id].score),
            }
            for index, item in enumerate(ordered, 1)
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, default=20)
    parser.add_argument("--fixtures", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "outputs" / "player_cards_demo")
    args = parser.parse_args()
    manifest = write_demo(args.output, args.players, args.fixtures)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
