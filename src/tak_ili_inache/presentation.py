from __future__ import annotations

import re
from decimal import Decimal

from .models import BetEvent, Fixture, Market, Round


TEAM_BUTTON_LIMIT = 14
MARKET_ORDER = (Market.P1, Market.X, Market.P2, Market.TB, Market.TM, Market.ONE_X, Market.X_TWO)


def compact_team_name(name: str, limit: int = TEAM_BUTTON_LIMIT) -> str:
    """Return a deterministic mobile label without hiding the team's identity."""
    normalized = " ".join(name.split())
    if len(normalized) <= limit:
        return normalized
    words = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", normalized)
    if len(words) > 1:
        initials = "".join(word[0].upper() for word in words[1:])
        candidate = f"{words[0]} {initials}"
        if len(candidate) <= limit:
            return candidate
    return f"{normalized[: max(1, limit - 1)].rstrip()}…"


def compact_match_label(fixture: Fixture, selected: bool = False) -> str:
    home = compact_team_name(fixture.home_team)
    away = compact_team_name(fixture.away_team)
    return f"{'✅ ' if selected else ''}{home} — {away} · {fixture.kickoff_msk:%d.%m}"


def event_market_label(event: BetEvent) -> str:
    if event.market in {Market.TB, Market.TM} and event.total_line_snapshot is not None:
        return f"{event.market.value} {_decimal_label(event.total_line_snapshot)}"
    return event.market.value


def public_event_label(round_: Round, event: BetEvent, *, include_odds: bool = False) -> str:
    fixture = next((item for item in round_.fixtures if item.match_id == event.match_id), None)
    match = f"{fixture.home_team} — {fixture.away_team}" if fixture else event.match_id
    label = f"{match} · {event_market_label(event)}"
    return f"{label} · {event.odds_snapshot}" if include_odds else label


def _decimal_label(value: Decimal) -> str:
    return format(value.normalize(), "f")
