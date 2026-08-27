from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import Market, Participant, Prediction, Round
from .presentation import MARKET_ORDER, compact_team_name, public_event_label
from .scoring import score_predictions

BET_TYPE_LABELS = {"single": "Ординары", "express": "Экспрессы"}


def build_public_coupons(
    output_dir: str | Path,
    round_: Round,
    predictions: tuple[Prediction, ...],
    participants: tuple[Participant, ...],
) -> Path:
    """Build one human-readable public file without internal identifiers."""
    names = {item.participant_id: item.display_name for item in participants}
    blocks = [f"Прогнозы тура {round_.round_id}", f"Участников: {len(predictions)}"]
    ordered = sorted(predictions, key=lambda item: (names.get(item.participant_id, ""), item.participant_id))
    for prediction in ordered:
        lines = [f"Купон: {names.get(prediction.participant_id, 'Участник')}"]
        for bet_no, bet in enumerate(prediction.bets, 1):
            bet_type = "Ординар" if bet.bet_type.value == "single" else "Экспресс"
            lines.append(f"{bet_no}. {bet_type} · {bet.stake}")
            lines.extend(f"   {public_event_label(round_, event, include_odds=True)}" for event in bet.events)
        blocks.append("\n".join(lines))
    content = "\n\n".join(blocks) + "\n"
    revision = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    directory = Path(output_dir) / "publication" / "bundles" / revision
    directory.mkdir(parents=True, exist_ok=True)
    safe_round_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in round_.round_id)[:48] or "round"
    path = directory / f"coupons_{safe_round_id}.txt"
    if not path.exists():
        with tempfile.NamedTemporaryFile("w", dir=directory, encoding="utf-8", delete=False) as target:
            temp = Path(target.name)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, path)
    return path


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
                        "display_name": names.get(prediction.participant_id, prediction.participant_id),
                        "submitted_at_msk": prediction.submitted_at_msk.isoformat(),
                        "bet_no": bet_no,
                        "bet_type": bet.bet_type.value,
                        "stake": bet.stake,
                        "combined_odds": str(combined_odds),
                        "potential_payout": int(payout),
                        "event_no": event_no,
                        "match_id": event.match_id,
                        "kickoff_msk": fixture.kickoff_msk.isoformat() if fixture else "",
                        "home_team": fixture.home_team if fixture else "",
                        "away_team": fixture.away_team if fixture else "",
                        "market": event.market.value,
                        "total_line": str(event.total_line_snapshot) if event.total_line_snapshot is not None else "",
                        "odds_snapshot": str(event.odds_snapshot),
                    }
                )
    revision = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    directory = Path(output_dir) / "prediction_exports"
    directory.mkdir(parents=True, exist_ok=True)
    safe_round_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in round_.round_id)[:48] or "round"
    path = directory / f"predictions_{safe_round_id}_{revision}.csv"
    if not path.exists():
        _write_csv_atomic(path, rows, _prediction_export_fields())
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
        json.dumps([round_.round_id, round_.checksum, rows], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    directory = Path(output_dir) / "publication" / "bundles" / revision
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "selection_popularity.png"
    if not path.exists():
        _selection_heatmap(path, round_, rows)
    return path, rows


def build_reports(output_dir: str | Path, round_id: str, predictions: tuple[Prediction, ...], results, participants: tuple[Participant, ...], scored_at: datetime) -> dict[str, Path]:
    scored, leaderboard = score_predictions(list(predictions), list(results))
    names = {item.participant_id: item.display_name for item in participants}
    by_participant = defaultdict(list)
    for row in scored:
        by_participant[row.participant_id].append(row)
    scoring_rows = []
    for row in scored:
        scoring_rows.append({"round_id": round_id, "participant_id": row.participant_id, "display_name": names.get(row.participant_id, row.participant_id), "bet_no": row.bet_no, "bet_type": row.bet_type.value, "stake": row.stake, "combined_odds": str(row.combined_odds), "is_win": str(row.is_win).lower(), "gross_payout": row.gross_payout, "net_result": row.gross_payout - row.stake, "event_details": _events(predictions, row.participant_id, row.bet_no), "scored_at": ""})
    leaderboard_rows = [{"round_id": round_id, "rank": row.rank, "participant_id": row.participant_id, "display_name": names.get(row.participant_id, row.participant_id), "gross_payout": row.gross_payout, "net_result": row.net_result, "winning_bets": row.winning_bets, "single_payout": sum(item.gross_payout for item in by_participant[row.participant_id] if item.bet_type.value == "single"), "express_payout": sum(item.gross_payout for item in by_participant[row.participant_id] if item.bet_type.value == "express")} for row in leaderboard]
    _verify_totals(scoring_rows, leaderboard_rows)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    revision = hashlib.sha256(json.dumps([scoring_rows, leaderboard_rows], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    bundle = directory / "bundles" / revision
    if not bundle.exists():
        bundle.parent.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=bundle.parent, prefix=".staging-"))
        paths = {"scoring": staging / "scoring.csv", "leaderboard": staging / "leaderboard.csv"}
        _write_csv(paths["scoring"], scoring_rows)
        _write_csv(paths["leaderboard"], leaderboard_rows)
        paths.update(_charts(staging, leaderboard_rows, scoring_rows))
        os.replace(staging, bundle)
    paths = {"scoring": bundle / "scoring.csv", "leaderboard": bundle / "leaderboard.csv"}
    paths.update({"chart_leaderboard": bundle / "chart_leaderboard.png", "chart_bet_types": bundle / "chart_bet_types.png", "chart_popularity": bundle / "chart_popularity.png"})
    _atomic_json(directory / "current.json", {"revision": revision, "generated_at": scored_at.isoformat()})
    return paths


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


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]) if rows else ["round_id"])
        writer.writeheader()
        writer.writerows(rows)


def _write_csv_atomic(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", newline="", delete=False) as target:
        temp = Path(target.name)
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp, path)


def _prediction_export_fields() -> list[str]:
    return [
        "round_id", "player_no", "display_name", "submitted_at_msk",
        "bet_no", "bet_type", "stake", "combined_odds", "potential_payout",
        "event_no", "match_id", "kickoff_msk", "home_team", "away_team",
        "market", "total_line", "odds_snapshot",
    ]


def _selection_heatmap(path: Path, round_: Round, rows: list[dict]) -> None:
    title_font, label_font = _fonts()
    left, top, cell_width, row_height = 430, 110, 112, 54
    width = left + cell_width * len(MARKET_ORDER) + 28
    height = top + row_height * len(round_.fixtures) + 40
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((28, 24), "Полная статистика выбора событий", font=title_font, fill="#18212f")
    draw.text((28, 66), f"Тур {round_.round_id}: число выборов по каждому матчу", font=label_font, fill="#52606d")
    for index, market in enumerate(MARKET_ORDER):
        x = left + index * cell_width
        draw.text((x + 36, top - 34), market.value, font=label_font, fill="#18212f")
    values = {(item["match_id"], item["market"]): int(item["count"]) for item in rows}
    maximum = max(values.values(), default=1)
    for row_no, fixture in enumerate(round_.fixtures):
        y = top + row_no * row_height
        match = f"{compact_team_name(fixture.home_team, 16)} — {compact_team_name(fixture.away_team, 16)}"
        draw.text((28, y + 14), _truncate(match, 34), font=label_font, fill="#18212f")
        for column, market in enumerate(MARKET_ORDER):
            value = values.get((fixture.match_id, market.value), 0)
            ratio = value / maximum if maximum else 0
            color = (int(240 - 155 * ratio), int(246 - 105 * ratio), 255)
            x = left + column * cell_width
            draw.rounded_rectangle((x, y + 5, x + cell_width - 10, y + row_height - 5), radius=7, fill=color)
            draw.text((x + 40, y + 14), str(value), font=label_font, fill="#18212f")
    image.save(path, format="PNG", optimize=True)


def _charts(directory: Path, leaderboard, scoring) -> dict[str, Path]:
    series = chart_series(leaderboard, scoring)
    charts = {
        "chart_leaderboard": directory / "chart_leaderboard.png",
        "chart_bet_types": directory / "chart_bet_types.png",
        "chart_popularity": directory / "chart_popularity.png",
    }
    _bar_chart(charts["chart_leaderboard"], "Рейтинг валовых выплат", series["chart_leaderboard"])
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


def _bar_chart(path: Path, title: str, values: list[tuple[str, int]]) -> None:
    """Dependency-minimal raster bar chart with a Unicode font present on Ubuntu 24.04."""
    height, width, top = max(220, 100 + len(values) * 42), 1200, 72
    maximum = max((value for _, value in values), default=1) or 1
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font = _fonts()
    draw.text((30, 22), title, font=title_font, fill="#18212f")
    for i, (label, value) in enumerate(values):
        y = top + i * 42
        safe_label = _truncate(label, 42)
        draw.text((30, y + 6), safe_label, font=label_font, fill="#18212f")
        bar = int(620 * value / maximum)
        draw.rounded_rectangle((430, y, 430 + bar, y + 28), radius=5, fill="#276ef1")
        draw.text((442 + bar, y + 5), str(value), font=label_font, fill="#18212f")
    image.save(path, format="PNG", optimize=True)


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, 26), ImageFont.truetype(candidate, 18)
    raise RuntimeError("Не найден Unicode-шрифт для PNG-графиков; установите fonts-dejavu-core.")


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit - 1]}…"
