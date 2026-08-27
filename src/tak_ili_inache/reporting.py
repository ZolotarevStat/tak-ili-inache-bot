from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import BetResult, Market, Participant, Prediction, Round
from .presentation import MARKET_ORDER, public_event_label
from .scoring import score_partial_bets, score_predictions

BET_TYPE_LABELS = {"single": "Ординары", "express": "Экспрессы"}

REPORT_RENDER_VERSION = "report-render-v4"
SELECTION_HEATMAP_RENDER_VERSION = "selection-heatmap-v4"
OUTCOME_HEATMAP_RENDER_VERSION = "outcome-heatmap-v3"
HEATMAP_TITLE_Y = 24
HEATMAP_SUBTITLE_Y = 66
HEATMAP_HEADER_Y = 112
HEATMAP_GRID_TOP = 146
OUTCOME_HEATMAP_LEGEND_Y = 102
OUTCOME_HEATMAP_HEADER_Y = 142
OUTCOME_HEATMAP_GRID_TOP = 176
OUTCOME_LEGEND = (
    ("зашло", "won", "#9fddb2"),
    ("не зашло", "lost", "#f3aaa5"),
    ("возврат", "returned", "#b9d4f5"),
    ("ожидается", "pending", "#e5e9ef"),
)
PENDING_OUTCOME_COLOR = (229, 233, 239)
OUTCOME_TARGETS = {
    "won": (84, 190, 116),
    "lost": (225, 92, 86),
    "returned": (100, 154, 220),
    "pending": (179, 188, 201),
}
HEATMAP_LABEL_X = 24
HEATMAP_LABEL_WIDTH = 260
HEATMAP_LABEL_GAP = 16
HEATMAP_LEFT = HEATMAP_LABEL_X + HEATMAP_LABEL_WIDTH + HEATMAP_LABEL_GAP
HEATMAP_CELL_WIDTH = 80
HEATMAP_ROW_HEIGHT = 47
PUBLIC_LEADERBOARD_FIELDS = (
    "round_id",
    "player_no",
    "rank",
    "display_name",
    "gross_payout",
    "net_result",
    "winning_bets",
    "single_payout",
    "express_payout",
)


@dataclass(frozen=True)
class HeatmapLayout:
    label_x: int
    label_width: int
    label_gap: int
    grid_x: int
    grid_top: int
    cell_width: int
    row_height: int
    width: int
    height: int


def build_public_coupons_csv(
    output_dir: str | Path,
    round_: Round,
    predictions: tuple[Prediction, ...],
    participants: tuple[Participant, ...],
) -> Path:
    """Build one Excel-friendly public CSV without internal identifiers."""
    names = {item.participant_id: item.display_name for item in participants}
    fixtures = {item.match_id: item for item in round_.fixtures}
    rows: list[dict] = []
    ordered = sorted(predictions, key=lambda item: (names.get(item.participant_id, ""), item.participant_id))
    for player_no, prediction in enumerate(ordered, 1):
        for bet_no, bet in enumerate(prediction.bets, 1):
            combined_odds = Decimal("1")
            for event in bet.events:
                combined_odds *= event.odds_snapshot
            potential = _rounded_payout(bet.stake, combined_odds)
            for event_no, event in enumerate(bet.events, 1):
                fixture = fixtures[event.match_id]
                rows.append(
                    {
                        "Тур": _csv_text(round_.round_id),
                        "Игрок №": player_no,
                        "Игрок": _csv_text(_public_name(names, prediction.participant_id)),
                        "Ставка №": bet_no,
                        "Тип": "Ординар" if bet.bet_type.value == "single" else "Экспресс",
                        "Сумма": bet.stake,
                        "Коэф. ставки": _decimal_csv(combined_odds),
                        "Потенциал": potential,
                        "Событие №": event_no,
                        "Начало МСК": fixture.kickoff_msk.strftime("%d.%m.%Y %H:%M"),
                        "Матч": _csv_text(f"{fixture.home_team} — {fixture.away_team}"),
                        "Исход": _market_label(event.market, event.total_line_snapshot),
                        "Коэф. события": _decimal_csv(event.odds_snapshot),
                    }
                )
    revision = hashlib.sha256(
        json.dumps(["public-csv-v2", round_.checksum, rows], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    directory = Path(output_dir) / "publication" / "bundles" / revision
    directory.mkdir(parents=True, exist_ok=True)
    safe_round_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in round_.round_id)[:48] or "round"
    path = directory / f"coupons_{safe_round_id}.csv"
    if not path.exists():
        _write_csv_atomic(path, rows, list(rows[0]) if rows else _public_coupon_fields(), encoding="utf-8-sig")
    return path


def build_interim_results_export(
    output_dir: str | Path,
    round_: Round,
    predictions: tuple[Prediction, ...],
    results: tuple[BetResult, ...],
    participants: tuple[Participant, ...],
) -> tuple[Path, list]:
    """Build a public interim CSV with realized and not-guaranteed ceilings."""
    names = {item.participant_id: item.display_name for item in participants}
    fixtures = {item.match_id: item for item in round_.fixtures}
    scored, leaderboard = score_partial_bets(list(predictions), list(results))
    by_bet = {(item.participant_id, item.bet_no): item for item in scored}
    by_player = {item.participant_id: item for item in leaderboard}
    rows: list[dict] = []
    ordered = sorted(
        predictions,
        key=lambda item: (by_player[item.participant_id].rank, names.get(item.participant_id, ""), item.participant_id),
    )
    for player_no, prediction in enumerate(ordered, 1):
        summary = by_player[prediction.participant_id]
        pending_ceiling = summary.maximum_payout - summary.realized_payout
        for bet_no, bet in enumerate(prediction.bets, 1):
            scored_bet = by_bet[(prediction.participant_id, bet_no)]
            for event_no, (event, event_status) in enumerate(zip(bet.events, scored_bet.event_statuses), 1):
                fixture = fixtures[event.match_id]
                rows.append(
                    {
                        "Тур": _csv_text(round_.round_id),
                        "Место": summary.rank,
                        "Игрок №": player_no,
                        "Игрок": _csv_text(_public_name(names, prediction.participant_id)),
                        "Начислено": summary.realized_payout,
                        "Макс. выплата ожидающих": pending_ceiling,
                        "Макс. итог": summary.maximum_payout,
                        "Ставка №": bet_no,
                        "Тип": "Ординар" if bet.bet_type.value == "single" else "Экспресс",
                        "Сумма": bet.stake,
                        "Статус ставки": _status_label(scored_bet.status),
                        "Выплата ставки": scored_bet.realized_payout,
                        "Макс. выплата ставки": scored_bet.maximum_payout,
                        "Событие №": event_no,
                        "Начало МСК": fixture.kickoff_msk.strftime("%d.%m.%Y %H:%M"),
                        "Матч": _csv_text(f"{fixture.home_team} — {fixture.away_team}"),
                        "Исход": _market_label(event.market, event.total_line_snapshot),
                        "Коэф. события": _decimal_csv(event.odds_snapshot),
                        "Статус события": _status_label(event_status),
                    }
                )
    normalized_results = [
        (item.match_id, sorted(market.value for market in item.winning_markets), sorted(market.value for market in item.returned_markets))
        for item in sorted(results, key=lambda item: item.match_id)
    ]
    revision = hashlib.sha256(
        json.dumps(["interim-csv-v2", round_.checksum, rows, normalized_results], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    directory = Path(output_dir) / "publication" / "interim" / revision
    directory.mkdir(parents=True, exist_ok=True)
    safe_round_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in round_.round_id)[:48] or "round"
    path = directory / f"interim_{safe_round_id}.csv"
    if not path.exists():
        _write_csv_atomic(path, rows, list(rows[0]) if rows else _interim_fields(), encoding="utf-8-sig")
    return path, leaderboard


def build_predictions_export(
    output_dir: str | Path,
    round_: Round,
    predictions: tuple[Prediction, ...],
    participants: tuple[Participant, ...],
) -> Path:
    """Build an admin-friendly long CSV without Telegram identifiers."""
    names = {item.participant_id: item.display_name for item in participants}
    fixtures = {item.match_id: item for item in round_.fixtures}
    rows: list[dict] = []
    ordered = sorted(predictions, key=lambda item: (names.get(item.participant_id, ""), item.participant_id))
    for player_no, prediction in enumerate(ordered, 1):
        for bet_no, bet in enumerate(prediction.bets, 1):
            combined_odds = Decimal("1")
            for event in bet.events:
                combined_odds *= event.odds_snapshot
            payout = (Decimal(bet.stake) * combined_odds).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            for event_no, event in enumerate(bet.events, 1):
                fixture = fixtures.get(event.match_id)
                rows.append(
                    {
                        "round_id": round_.round_id,
                        "player_no": player_no,
                        "display_name": _public_name(names, prediction.participant_id),
                        "submitted_at_msk": prediction.submitted_at_msk.isoformat(),
                        "bet_no": bet_no,
                        "bet_type": bet.bet_type.value,
                        "stake": bet.stake,
                        "combined_odds": _decimal_csv(combined_odds),
                        "potential_payout": int(payout),
                        "event_no": event_no,
                        "match_id": event.match_id,
                        "kickoff_msk": fixture.kickoff_msk.isoformat() if fixture else "",
                        "home_team": fixture.home_team if fixture else "",
                        "away_team": fixture.away_team if fixture else "",
                        "market": event.market.value,
                        "total_line": _decimal_csv(event.total_line_snapshot) if event.total_line_snapshot is not None else "",
                        "odds_snapshot": _decimal_csv(event.odds_snapshot),
                    }
                )
    revision = hashlib.sha256(
        json.dumps(["prediction-export-v2", rows], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    directory = Path(output_dir) / "prediction_exports"
    directory.mkdir(parents=True, exist_ok=True)
    safe_round_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in round_.round_id)[:48] or "round"
    path = directory / f"predictions_{safe_round_id}_{revision}.csv"
    if not path.exists():
        _write_csv_atomic(path, rows, _prediction_export_fields(), encoding="utf-8-sig")
    return path


def selection_popularity(round_: Round, predictions: tuple[Prediction, ...]) -> list[dict]:
    counts: Counter = Counter()
    bank: Counter = Counter()
    sample_events = {}
    for prediction in predictions:
        for bet in prediction.bets:
            for event in bet.events:
                key = (event.match_id, event.market)
                counts[key] += 1
                bank[key] += bet.stake
                sample_events.setdefault(key, event)
    rows = []
    for fixture in round_.fixtures:
        for market in MARKET_ORDER:
            key = (fixture.match_id, market)
            if not counts[key]:
                continue
            event = sample_events[key]
            rows.append(
                {
                    "match_id": fixture.match_id,
                    "market": market.value,
                    "label": public_event_label(round_, event),
                    "count": counts[key],
                    "bank": bank[key],
                }
            )
    return sorted(rows, key=lambda item: (-item["count"], item["label"]))


def build_popularity_chart(
    output_dir: str | Path,
    round_: Round,
    predictions: tuple[Prediction, ...],
) -> tuple[Path, list[dict]]:
    rows = selection_popularity(round_, predictions)
    revision = hashlib.sha256(
        json.dumps([SELECTION_HEATMAP_RENDER_VERSION, round_.round_id, round_.checksum, rows], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    directory = Path(output_dir) / "publication" / "bundles" / revision
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "selection_popularity.png"
    if not path.exists():
        _selection_heatmap(path, round_, rows)
    return path, rows


def build_outcome_chart(
    output_dir: str | Path,
    round_: Round,
    predictions: tuple[Prediction, ...],
    results: tuple[BetResult, ...],
    *,
    final: bool = False,
) -> Path:
    """Build a result-aware counterpart of the selection heatmap.

    A cell keeps the original selection count, while its colour reflects the
    settled event outcome shared by every participant who chose that market.
    """
    rows = selection_popularity(round_, predictions)
    normalized_results = [
        (
            item.match_id,
            sorted(market.value for market in item.winning_markets),
            sorted(market.value for market in item.returned_markets),
        )
        for item in sorted(results, key=lambda item: item.match_id)
    ]
    revision = hashlib.sha256(
        json.dumps(
            [OUTCOME_HEATMAP_RENDER_VERSION, round_.round_id, round_.checksum, rows, normalized_results, final],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    kind = "final" if final else "interim"
    directory = Path(output_dir) / "publication" / kind / revision
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "event_outcomes.png"
    if not path.exists():
        _outcome_heatmap(path, round_, rows, results, final=final)
    return path


def build_reports(output_dir: str | Path, round_id: str, predictions: tuple[Prediction, ...], results, participants: tuple[Participant, ...], scored_at: datetime) -> dict[str, Path]:
    scored, leaderboard = score_predictions(list(predictions), list(results))
    names = {item.participant_id: item.display_name for item in participants}
    by_participant = defaultdict(list)
    for row in scored:
        by_participant[row.participant_id].append(row)
    scoring_rows = []
    for row in scored:
        scoring_rows.append({"round_id": _csv_text(round_id), "participant_id": row.participant_id, "display_name": _csv_text(names.get(row.participant_id, row.participant_id)), "bet_no": row.bet_no, "bet_type": row.bet_type.value, "stake": row.stake, "combined_odds": _decimal_csv(row.combined_odds), "is_win": str(row.is_win).lower(), "gross_payout": row.gross_payout, "net_result": row.gross_payout - row.stake, "event_details": _events(predictions, row.participant_id, row.bet_no), "scored_at": ""})
    leaderboard_rows = [{"round_id": _csv_text(round_id), "rank": row.rank, "participant_id": row.participant_id, "display_name": _csv_text(names.get(row.participant_id, row.participant_id)), "gross_payout": row.gross_payout, "net_result": row.net_result, "winning_bets": row.winning_bets, "single_payout": sum(item.gross_payout for item in by_participant[row.participant_id] if item.bet_type.value == "single"), "express_payout": sum(item.gross_payout for item in by_participant[row.participant_id] if item.bet_type.value == "express")} for row in leaderboard]
    public_leaderboard_rows = _public_leaderboard_rows(leaderboard_rows, names)
    _verify_totals(scoring_rows, leaderboard_rows)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    revision = hashlib.sha256(json.dumps([REPORT_RENDER_VERSION, scoring_rows, leaderboard_rows, public_leaderboard_rows], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    bundle = directory / "bundles" / revision
    if not bundle.exists():
        bundle.parent.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=bundle.parent, prefix=".staging-"))
        paths = {
            "scoring": staging / "scoring.csv",
            "leaderboard": staging / "leaderboard.csv",
            "public_leaderboard": staging / "final_leaderboard.csv",
        }
        _write_csv(paths["scoring"], scoring_rows, encoding="utf-8-sig")
        _write_csv(paths["leaderboard"], leaderboard_rows, encoding="utf-8-sig")
        _write_csv(paths["public_leaderboard"], public_leaderboard_rows, fieldnames=PUBLIC_LEADERBOARD_FIELDS, encoding="utf-8-sig")
        paths.update(_charts(staging, leaderboard_rows, scoring_rows))
        os.replace(staging, bundle)
    paths = {
        "scoring": bundle / "scoring.csv",
        "leaderboard": bundle / "leaderboard.csv",
        "public_leaderboard": bundle / "final_leaderboard.csv",
    }
    paths.update({"chart_leaderboard": bundle / "chart_leaderboard.png", "chart_bet_types": bundle / "chart_bet_types.png", "chart_popularity": bundle / "chart_popularity.png"})
    _atomic_json(directory / "current.json", {"revision": revision, "generated_at": scored_at.isoformat()})
    return paths


def _public_leaderboard_rows(leaderboard_rows: list[dict], names: dict[str, str]) -> list[dict]:
    """Presentation-only final standings: deliberately no repository identity."""
    rows: list[dict] = []
    for player_no, item in enumerate(leaderboard_rows, 1):
        display_name = names.get(item["participant_id"], f"Участник №{player_no}")
        rows.append(
            {
                "round_id": item["round_id"],
                "player_no": player_no,
                "rank": item["rank"],
                "display_name": _csv_text(display_name),
                "gross_payout": item["gross_payout"],
                "net_result": item["net_result"],
                "winning_bets": item["winning_bets"],
                "single_payout": item["single_payout"],
                "express_payout": item["express_payout"],
            }
        )
    return rows


def _atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as target:
        temp = Path(target.name)
        json.dump(value, target, ensure_ascii=False)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp, path)


def _events(predictions, participant_id, bet_no) -> str:
    prediction = next(item for item in predictions if item.participant_id == participant_id)
    return "; ".join(f"{event.match_id}:{event.market.value}" for event in prediction.bets[bet_no - 1].events)


def _verify_totals(scoring_rows, leaderboard_rows) -> None:
    totals = Counter()
    for row in scoring_rows:
        totals[row["participant_id"]] += int(row["gross_payout"])
    for row in leaderboard_rows:
        if totals[row["participant_id"]] != int(row["gross_payout"]):
            raise ValueError("scoring and leaderboard totals differ")


def _write_csv(
    path: Path, rows: list[dict], *, fieldnames: tuple[str, ...] | list[str] | None = None, encoding: str = "utf-8"
) -> None:
    with path.open("w", encoding=encoding, newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(fieldnames or (list(rows[0]) if rows else ["round_id"])))
        writer.writeheader()
        writer.writerows(_safe_csv_row(row) for row in rows)


def _write_csv_atomic(
    path: Path, rows: list[dict], fieldnames: list[str], encoding: str = "utf-8"
) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding=encoding, newline="", delete=False) as target:
        temp = Path(target.name)
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(_safe_csv_row(row) for row in rows)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp, path)


def _rounded_payout(stake: int, odds: Decimal) -> int:
    return int((Decimal(stake) * odds).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decimal_csv(value: Decimal) -> str:
    """Keep Decimal exact while avoiding ``m.d`` date coercion in Russian Excel."""
    return format(value, "f").replace(".", ",")


def _csv_text(value: str) -> str:
    """Neutralize spreadsheet formulas in externally supplied text cells."""
    value = value.replace("\r", " ").replace("\n", " ")
    # Keep a leading tab observable: ``str.lstrip()`` would erase it before
    # the formula-prefix check, while Excel still treats it as significant.
    if value.lstrip(" ").startswith(("=", "+", "-", "@", "\t")):
        return "'" + value
    return value


def _safe_csv_row(row: dict) -> dict:
    """Apply the same text-cell contract to every public and admin CSV writer."""
    return {key: _csv_text(value) if isinstance(value, str) else value for key, value in row.items()}


def _public_name(names: dict[str, str], participant_id: str) -> str:
    name = names.get(participant_id)
    if not name:
        raise ValueError("participant display name missing")
    return name


def _market_label(market: Market, total_line: Decimal | None) -> str:
    if market in {Market.TB, Market.TM} and total_line is not None:
        return f"{market.value} {_decimal_csv(total_line)}"
    return market.value


def _status_label(status: str) -> str:
    return {
        "won": "зашло",
        "lost": "не зашло",
        "returned": "возврат",
        "pending": "ожидается",
    }[status]


def _public_coupon_fields() -> list[str]:
    return [
        "Тур", "Игрок №", "Игрок", "Ставка №", "Тип", "Сумма",
        "Коэф. ставки", "Потенциал", "Событие №", "Начало МСК", "Матч",
        "Исход", "Коэф. события",
    ]


def _interim_fields() -> list[str]:
    return [
        "Тур", "Место", "Игрок №", "Игрок", "Начислено",
        "Макс. выплата ожидающих", "Макс. итог", "Ставка №", "Тип",
        "Сумма", "Статус ставки", "Выплата ставки", "Макс. выплата ставки",
        "Событие №", "Начало МСК", "Матч", "Исход", "Коэф. события",
        "Статус события",
    ]


def _prediction_export_fields() -> list[str]:
    return [
        "round_id", "player_no", "display_name", "submitted_at_msk",
        "bet_no", "bet_type", "stake", "combined_odds", "potential_payout",
        "event_no", "match_id", "kickoff_msk", "home_team", "away_team",
        "market", "total_line", "odds_snapshot",
    ]


def _heatmap_layout(grid_top: int, fixture_count: int, bottom_padding: int) -> HeatmapLayout:
    """Shared mobile layout: labels, a real gap, then the immutable market grid."""
    grid_x = HEATMAP_LEFT
    return HeatmapLayout(
        label_x=HEATMAP_LABEL_X,
        label_width=HEATMAP_LABEL_WIDTH,
        label_gap=HEATMAP_LABEL_GAP,
        grid_x=grid_x,
        grid_top=grid_top,
        cell_width=HEATMAP_CELL_WIDTH,
        row_height=HEATMAP_ROW_HEIGHT,
        width=grid_x + HEATMAP_CELL_WIDTH * len(MARKET_ORDER) + 28,
        height=grid_top + HEATMAP_ROW_HEIGHT * fixture_count + bottom_padding,
    )


def _draw_heatmap_label(
    draw: ImageDraw.ImageDraw,
    fixture,
    row_y: int,
    layout: HeatmapLayout,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, int, int]:
    label = _fixture_label(fixture, font, layout.label_width)
    position = (layout.label_x, row_y + 14)
    bbox = draw.textbbox(position, label, font=font)
    if bbox[2] > layout.label_x + layout.label_width:
        raise AssertionError("heatmap fixture label crossed the label zone")
    if bbox[2] + layout.label_gap > layout.grid_x:
        raise AssertionError("heatmap fixture label crossed the grid gap")
    draw.text(position, label, font=font, fill="#18212f")
    return bbox


def _fixture_label(fixture, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    return _truncate_pixels(f"{fixture.home_team} — {fixture.away_team}", font, max_width)


def _truncate_pixels(value: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """One-line ellipsis that obeys rendered, not character, width."""
    if font.getlength(value) <= max_width:
        return value
    ellipsis = "…"
    available = max_width - font.getlength(ellipsis)
    if available <= 0:
        return ellipsis
    end = len(value)
    while end and font.getlength(value[:end]) > available:
        end -= 1
    return value[:end].rstrip() + ellipsis


def _selection_heatmap(path: Path, round_: Round, rows: list[dict]) -> None:
    title_font, bold_font, regular_font = _fonts()
    layout = _heatmap_layout(HEATMAP_GRID_TOP, len(round_.fixtures), 40)
    image = Image.new("RGB", (layout.width, layout.height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((28, HEATMAP_TITLE_Y), "Полная статистика выбора событий", font=title_font, fill="#18212f")
    draw.text((28, HEATMAP_SUBTITLE_Y), f"Тур {round_.round_id}: число выборов по каждому матчу", font=regular_font, fill="#52606d")
    for index, market in enumerate(MARKET_ORDER):
        x = layout.grid_x + index * layout.cell_width
        draw.text((_cell_text_x(x, layout.cell_width, market.value, bold_font), HEATMAP_HEADER_Y), market.value, font=bold_font, fill="#18212f")
    values = {(item["match_id"], item["market"]): int(item["count"]) for item in rows}
    maximum = max(values.values(), default=1)
    for row_no, fixture in enumerate(round_.fixtures):
        y = layout.grid_top + row_no * layout.row_height
        _draw_heatmap_label(draw, fixture, y, layout, bold_font)
        for column, market in enumerate(MARKET_ORDER):
            value = values.get((fixture.match_id, market.value), 0)
            ratio = value / maximum if maximum else 0
            color = (int(240 - 155 * ratio), int(246 - 105 * ratio), 255)
            x = layout.grid_x + column * layout.cell_width
            draw.rounded_rectangle((x, y + 5, x + layout.cell_width - 10, y + layout.row_height - 5), radius=7, fill=color)
            draw.text((_cell_text_x(x, layout.cell_width, str(value), regular_font), y + 13), str(value), font=regular_font, fill="#18212f")
    image.save(path, format="PNG", optimize=True)


def _outcome_heatmap(
    path: Path,
    round_: Round,
    rows: list[dict],
    results: tuple[BetResult, ...],
    *,
    final: bool,
) -> None:
    title_font, bold_font, regular_font = _fonts()
    layout = _heatmap_layout(OUTCOME_HEATMAP_GRID_TOP, len(round_.fixtures), 54)
    image = Image.new("RGB", (layout.width, layout.height), "white")
    draw = ImageDraw.Draw(image)
    title = "Итоги выбранных событий" if final else "Промежуточные итоги событий"
    draw.text((28, HEATMAP_TITLE_Y), title, font=title_font, fill="#18212f")
    draw.text(
        (28, HEATMAP_SUBTITLE_Y),
        f"Тур {round_.round_id}: число выборов; цвет показывает исход события",
        font=regular_font,
        fill="#52606d",
    )
    legend_x = 28
    for label, _, color in OUTCOME_LEGEND:
        draw.rounded_rectangle(
            (legend_x, OUTCOME_HEATMAP_LEGEND_Y, legend_x + 22, OUTCOME_HEATMAP_LEGEND_Y + 22),
            radius=4,
            fill=color,
        )
        draw.text((legend_x + 30, OUTCOME_HEATMAP_LEGEND_Y), label, font=regular_font, fill="#18212f")
        legend_x += 30 + regular_font.getlength(label) + 28
    for index, market in enumerate(MARKET_ORDER):
        x = layout.grid_x + index * layout.cell_width
        draw.text((_cell_text_x(x, layout.cell_width, market.value, bold_font), OUTCOME_HEATMAP_HEADER_Y), market.value, font=bold_font, fill="#18212f")

    values = {(item["match_id"], item["market"]): int(item["count"]) for item in rows}
    result_by_match = {item.match_id: item for item in results}
    maximum = max(values.values(), default=1)
    for row_no, fixture in enumerate(round_.fixtures):
        y = layout.grid_top + row_no * layout.row_height
        _draw_heatmap_label(draw, fixture, y, layout, bold_font)
        result = result_by_match.get(fixture.match_id)
        for column, market in enumerate(MARKET_ORDER):
            value = values.get((fixture.match_id, market.value), 0)
            status = _event_outcome_status(result, market)
            color = _outcome_cell_color(status, value / maximum if maximum else 0)
            x = layout.grid_x + column * layout.cell_width
            draw.rounded_rectangle((x, y + 5, x + layout.cell_width - 10, y + layout.row_height - 5), radius=7, fill=color)
            draw.text((_cell_text_x(x, layout.cell_width, str(value), regular_font), y + 13), str(value), font=regular_font, fill="#18212f")
    if final and len(result_by_match) != len(round_.fixtures):
        raise ValueError("final outcome heatmap requires every match result")
    image.save(path, format="PNG", optimize=True)


def _event_outcome_status(result: BetResult | None, market: Market) -> str:
    if result is None:
        return "pending"
    if market in result.returned_markets:
        return "returned"
    if market in result.winning_markets:
        return "won"
    return "lost"


def _outcome_cell_color(status: str, ratio: float) -> tuple[int, int, int]:
    if status == "pending":
        return PENDING_OUTCOME_COLOR if ratio <= 0 else _blend_outcome(OUTCOME_TARGETS[status], 0.35 + 0.65 * ratio)
    # A zero still has a factual event state.  Keep it visibly distinct from
    # a pending event while making the absence of selections clear via "0".
    return _blend_outcome(OUTCOME_TARGETS[status], 0.14 + 0.86 * max(0.0, ratio))


def _blend_outcome(target: tuple[int, int, int], strength: float) -> tuple[int, int, int]:
    return tuple(int(255 - (255 - channel) * strength) for channel in target)


def _cell_text_x(x: int, cell_width: int, value: str, font: ImageFont.FreeTypeFont) -> int:
    return int(x + (cell_width - 10 - font.getlength(value)) / 2)


def _charts(directory: Path, leaderboard, scoring) -> dict[str, Path]:
    series = chart_series(leaderboard, scoring)
    charts = {
        "chart_leaderboard": directory / "chart_leaderboard.png",
        "chart_bet_types": directory / "chart_bet_types.png",
        "chart_popularity": directory / "chart_popularity.png",
    }
    _bar_chart(charts["chart_leaderboard"], "Рейтинг валовых выплат", series["chart_leaderboard"], bold_labels=True)
    _bar_chart(charts["chart_bet_types"], "Выплаты: ординары и экспрессы", series["chart_bet_types"])
    _bar_chart(charts["chart_popularity"], "Популярность событий / банк", series["chart_popularity"])
    return charts


def chart_series(leaderboard: list[dict], scoring: list[dict]) -> dict[str, list[tuple[str, int]]]:
    """Return the exact CSV-derived series used by all report charts."""
    payout_by_type = Counter()
    event_popularity, bank_by_event = Counter(), Counter()
    for row in scoring:
        payout_by_type[row["bet_type"]] += int(row["gross_payout"])
        for event in row["event_details"].split("; "):
            event_popularity[event] += 1
            bank_by_event[event] += int(row["stake"])
    return {
        "chart_leaderboard": [(item["display_name"], int(item["gross_payout"])) for item in leaderboard],
        "chart_bet_types": [(BET_TYPE_LABELS[key], value) for key, value in payout_by_type.items()],
        "chart_popularity": [
            (f"{key} ({bank_by_event[key]})", value)
            for key, value in event_popularity.most_common(12)
        ],
    }


def _bar_chart(path: Path, title: str, values: list[tuple[str, int]], *, bold_labels: bool = False) -> None:
    """Dependency-minimal raster bar chart with a Unicode font present on Ubuntu 24.04."""
    height, width, top = max(220, 100 + len(values) * 42), 1200, 72
    maximum = max((value for _, value in values), default=1) or 1
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, bold_font, regular_font = _fonts()
    draw.text((30, 22), title, font=title_font, fill="#18212f")
    for i, (label, value) in enumerate(values):
        y = top + i * 42
        safe_label = _truncate(label, 42)
        draw.text((30, y + 6), safe_label, font=bold_font if bold_labels else regular_font, fill="#18212f")
        bar = int(620 * value / maximum)
        draw.rounded_rectangle((430, y, 430 + bar, y + 28), radius=5, fill="#276ef1")
        draw.text((442 + bar, y + 5), str(value), font=regular_font, fill="#18212f")
    image.save(path, format="PNG", optimize=True)


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    families = (
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ),
        (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        ),
    )
    for bold_path, regular_path in families:
        if Path(bold_path).is_file() and Path(regular_path).is_file():
            return (
                ImageFont.truetype(bold_path, 26),
                ImageFont.truetype(bold_path, 18),
                ImageFont.truetype(regular_path, 18),
            )
    raise RuntimeError("Не найден Unicode-шрифт для PNG-графиков; установите fonts-dejavu-core.")


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}…"
