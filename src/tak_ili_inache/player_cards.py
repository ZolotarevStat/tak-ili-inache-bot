from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
from typing import Sequence, TypeVar

from PIL import Image, ImageDraw, ImageFont

from .models import Bet, BetResult, BetType, Market, Participant, Prediction, Round
from .reporting import RenderedPng
from .scoring import score_partial_bets


CARD_WIDTH = 1080
CARD_HEIGHT = 1080
BET_TOP = 280
BET_GAP = 8
FOOTER_TOP = 913
REGULAR_BET_BASE = 78
REGULAR_EVENT_STEP = 26
REGULAR_EVENT_OFFSET = 53
COMPACT_BET_BASE = 66
COMPACT_EVENT_STEP = 22
COMPACT_EVENT_OFFSET = 48
MATCH_WEIGHT = Decimal("0.70")
OUTCOME_WEIGHT = Decimal("0.30")
STATUS_LABELS = {
    "won": "ЗАШЛО",
    "lost": "НЕ ЗАШЛО",
    "returned": "ВОЗВРАТ",
    "pending": "ОЖИДАЕТСЯ",
}
STATUS_COLORS = {
    "won": "#31C878",
    "lost": "#F06464",
    "returned": "#62A5F5",
    "pending": "#A8B1C2",
}


@dataclass(frozen=True)
class NearestNeighbour:
    participant_id: str | None
    score: Decimal


T = TypeVar("T")


def _event_map(prediction: Prediction) -> dict[str, Market]:
    return {
        event.match_id: event.market
        for bet in prediction.bets
        for event in bet.events
    }


def prediction_similarity(first: Prediction, second: Prediction) -> Decimal:
    """Return weighted match/market similarity in the closed interval [0, 1]."""
    first_events, second_events = _event_map(first), _event_map(second)
    union = set(first_events) | set(second_events)
    if not union:
        return Decimal("1")
    shared = set(first_events) & set(second_events)
    exact = sum(first_events[match_id] == second_events[match_id] for match_id in shared)
    denominator = Decimal(len(union))
    score = MATCH_WEIGHT * Decimal(len(shared)) / denominator
    score += OUTCOME_WEIGHT * Decimal(exact) / denominator
    return score.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def nearest_neighbours(
    predictions: Sequence[Prediction],
    names: dict[str, str],
) -> dict[str, NearestNeighbour]:
    result: dict[str, NearestNeighbour] = {}
    for prediction in predictions:
        candidates = [item for item in predictions if item.participant_id != prediction.participant_id]
        if not candidates:
            result[prediction.participant_id] = NearestNeighbour(None, Decimal("0"))
            continue
        neighbour = min(
            candidates,
            key=lambda item: (
                -prediction_similarity(prediction, item),
                names.get(item.participant_id, item.participant_id).casefold(),
                item.participant_id,
            ),
        )
        result[prediction.participant_id] = NearestNeighbour(
            neighbour.participant_id,
            prediction_similarity(prediction, neighbour),
        )
    return result


def similarity_order(
    predictions: Sequence[Prediction],
    names: dict[str, str],
) -> tuple[Prediction, ...]:
    """Create a deterministic path whose adjacent cards are locally similar."""
    if len(predictions) < 2:
        return tuple(predictions)
    pairs = []
    for left_index, left in enumerate(predictions):
        for right in predictions[left_index + 1:]:
            labels = tuple(sorted((
                names.get(left.participant_id, left.participant_id),
                names.get(right.participant_id, right.participant_id),
            )))
            pairs.append((prediction_similarity(left, right), labels, left, right))
    _, _, first, second = min(
        pairs,
        key=lambda item: (-item[0], tuple(label.casefold() for label in item[1])),
    )
    chain = [first, second]
    remaining = {
        item.participant_id: item
        for item in predictions
        if item.participant_id not in {first.participant_id, second.participant_id}
    }
    while remaining:
        options = []
        for candidate in remaining.values():
            for side, endpoint in (("left", chain[0]), ("right", chain[-1])):
                options.append((
                    prediction_similarity(candidate, endpoint),
                    names.get(candidate.participant_id, candidate.participant_id).casefold(),
                    side,
                    candidate,
                ))
        _, _, side, candidate = min(options, key=lambda item: (-item[0], item[1], item[2]))
        if side == "left":
            chain.insert(0, candidate)
        else:
            chain.append(candidate)
        remaining.pop(candidate.participant_id)
    return tuple(chain)


def media_batches(items: Sequence[T], maximum: int = 10) -> tuple[tuple[T, ...], ...]:
    """Split media into Telegram-sized groups and avoid a one-item tail."""
    if maximum < 2:
        raise ValueError("maximum must be at least 2")
    batches = [list(items[offset:offset + maximum]) for offset in range(0, len(items), maximum)]
    if len(batches) > 1 and len(batches[-1]) == 1:
        batches[-1].insert(0, batches[-2].pop())
    return tuple(tuple(batch) for batch in batches)


def build_player_cards(
    round_: Round,
    predictions: Sequence[Prediction],
    results: Sequence[BetResult],
    participants: Sequence[Participant],
) -> tuple[RenderedPng, ...]:
    names = {item.participant_id: item.display_name for item in participants}
    missing_names = sorted({item.participant_id for item in predictions} - names.keys())
    if missing_names:
        raise ValueError("Every prediction must belong to a registered participant.")
    ordered = similarity_order(predictions, names)
    neighbours = nearest_neighbours(predictions, names)
    partial_bets, leaderboard = score_partial_bets(list(predictions), list(results))
    scored = {(item.participant_id, item.bet_no): item for item in partial_bets}
    boards = {item.participant_id: item for item in leaderboard}
    fixtures = {item.match_id: item for item in round_.fixtures}
    cards = []
    for card_no, prediction in enumerate(ordered, 1):
        neighbour = neighbours[prediction.participant_id]
        neighbour_name = names.get(neighbour.participant_id or "", "—")
        cards.append(RenderedPng(
            f"player-card-{card_no:02}.png",
            _render_card(
                round_, prediction, names[prediction.participant_id], fixtures,
                scored, boards[prediction.participant_id], neighbour_name,
                neighbour.score, card_no, len(ordered),
            ),
        ))
    return tuple(cards)


def card_bet_boxes(prediction: Prediction) -> tuple[tuple[int, int], ...]:
    """Return compact non-overlapping vertical boxes for every bet."""
    boxes, _, _ = _card_bet_layout(prediction)
    return boxes


def _card_bet_layout(prediction: Prediction) -> tuple[tuple[tuple[int, int], ...], int, int]:
    """Choose the roomiest layout that still supports every valid coupon shape."""
    event_counts = [max(1, len(bet.events)) for bet in prediction.bets]
    available = FOOTER_TOP - 24 - BET_TOP
    gaps = BET_GAP * max(0, len(event_counts) - 1)
    preferred = sum(REGULAR_BET_BASE + REGULAR_EVENT_STEP * count for count in event_counts) + gaps
    if preferred <= available:
        base, event_step, event_offset = REGULAR_BET_BASE, REGULAR_EVENT_STEP, REGULAR_EVENT_OFFSET
    else:
        base, event_step, event_offset = COMPACT_BET_BASE, COMPACT_EVENT_STEP, COMPACT_EVENT_OFFSET
    boxes = []
    top = BET_TOP
    for event_count in event_counts:
        height = base + event_step * event_count
        boxes.append((top, top + height))
        top += height + BET_GAP
    if boxes and boxes[-1][1] > FOOTER_TOP - 24:
        raise AssertionError("bet cards overlap the nearest-neighbour footer")
    return tuple(boxes), event_offset, event_step


def _render_card(
    round_: Round,
    prediction: Prediction,
    display_name: str,
    fixtures: dict,
    scored: dict,
    board,
    neighbour_name: str,
    neighbour_score: Decimal,
    card_no: int,
    card_count: int,
) -> bytes:
    fonts = _fonts()
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), "#101521")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (34, 24, CARD_WIDTH - 34, CARD_HEIGHT - 24),
        radius=38, fill="#171E2D", outline="#2A3448", width=2,
    )
    draw.text((70, 48), "ТАК ИЛИ ИНАЧЕ  ·  ПОДРОБНАЯ СТАТИСТИКА", font=fonts["small_bold"], fill="#8EA0BE")
    draw.text((70, 82), _fit(display_name, 25), font=fonts["title"], fill="#F7F9FC")
    draw.text((70, 145), f"Тур {round_.round_id}  ·  карточка {card_no}/{card_count}", font=fonts["body"], fill="#9EABC1")

    summary_y = 181
    _summary_box(draw, (70, summary_y, 360, summary_y + 80), "НАЧИСЛЕНО", _money(board.realized_payout), "#31C878", fonts)
    _summary_box(draw, (380, summary_y, 690, summary_y + 80), "МАКС. ИТОГ", _money(board.maximum_payout), "#F2C94C", fonts)
    _summary_box(
        draw, (710, summary_y, 1010, summary_y + 80), "ЗАШЛО / МИМО / ЖИВЫ",
        f"{board.winning_bets} / {board.losing_bets} / {board.pending_bets}", "#62A5F5", fonts,
    )

    bet_boxes, event_offset, event_step = _card_bet_layout(prediction)
    for bet_no, (bet, (y, block_bottom)) in enumerate(zip(prediction.bets, bet_boxes), 1):
        item = scored[(prediction.participant_id, bet_no)]
        color = STATUS_COLORS[item.status]
        draw.rounded_rectangle((62, y, 1018, block_bottom), radius=22, fill="#20293A")
        draw.rounded_rectangle((62, y, 72, block_bottom), radius=5, fill=color)
        kind = "ОРДИНАР" if bet.bet_type == BetType.SINGLE else "ЭКСПРЕСС"
        draw.text((92, y + 12), f"{bet_no}. {kind}  ·  {bet.stake} р.", font=fonts["bet"], fill="#F7F9FC")
        badge = STATUS_LABELS[item.status]
        badge_width = fonts["small_bold"].getlength(badge) + 30
        draw.rounded_rectangle((985 - badge_width, y + 10, 1000, y + 44), radius=12, fill=color)
        draw.text((1000 - badge_width + 15, y + 18), badge, font=fonts["tiny_bold"], fill="#101521")
        payout = (
            f"выплата {item.realized_payout:,} р." if item.status in {"won", "returned"}
            else f"до {item.maximum_payout:,} р." if item.status == "pending"
            else "выплата 0 р."
        ).replace(",", " ")
        odds_and_payout = f"кэф {_fmt_decimal(_combined_odds(bet))} · {payout}"
        odds_width = fonts["small_bold"].getlength(odds_and_payout)
        draw.text((965 - badge_width - odds_width, y + 17), odds_and_payout, font=fonts["small_bold"], fill=color)
        for event_no, (event, event_status) in enumerate(zip(bet.events, item.event_statuses)):
            fixture = fixtures[event.match_id]
            label = f"{fixture.home_team} — {fixture.away_team}"
            market = event.market.value + (f" {event.total_line_snapshot}" if event.total_line_snapshot is not None else "")
            row_y = y + event_offset + event_no * event_step
            draw.ellipse((94, row_y + 5, 108, row_y + 19), fill=STATUS_COLORS[event_status])
            draw.text((123, row_y), _fit(label, 34), font=fonts["small"], fill="#DDE4EF")
            draw.text((590, row_y), f"{market} · {event.odds_snapshot}", font=fonts["small"], fill="#AEBBD0")

    draw.text((70, FOOTER_TOP), "БЛИЖАЙШИЙ ПРОГНОЗ", font=fonts["tiny_bold"], fill="#8493AC")
    neighbour_line = f"{_fit(neighbour_name, 22)}  ·  {neighbour_score:.2f}"
    line_width = fonts["footer"].getlength(neighbour_line)
    draw.text((1010 - line_width, FOOTER_TOP - 4), neighbour_line, font=fonts["footer"], fill="#F7F9FC")
    bar_y = 978
    draw.rounded_rectangle((70, bar_y, 1010, bar_y + 18), radius=9, fill="#2B3547")
    fill_width = int(940 * float(neighbour_score))
    if fill_width:
        draw.rounded_rectangle((70, bar_y, 70 + fill_width, bar_y + 18), radius=9, fill="#8B7CFF")
    return _png_bytes(image)


def _summary_box(draw, box, label: str, value: str, accent: str, fonts: dict) -> None:
    draw.rounded_rectangle(box, radius=18, fill="#20293A")
    draw.text((box[0] + 20, box[1] + 11), label, font=fonts["tiny_bold"], fill="#8493AC")
    draw.text((box[0] + 20, box[1] + 36), value, font=fonts["summary"], fill=accent)


def _combined_odds(bet: Bet) -> Decimal:
    result = Decimal("1")
    for event in bet.events:
        result *= event.odds_snapshot
    return result


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _money(value: int) -> str:
    return f"{value:,} р.".replace(",", " ")


def _fit(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


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
