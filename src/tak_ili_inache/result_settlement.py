from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import BetResult, Fixture, Market


@dataclass(frozen=True)
class GoalChoice:
    minimum: int
    is_five_plus: bool = False

    @classmethod
    def from_token(cls, token: str) -> "GoalChoice":
        if token == "5p":
            return cls(5, True)
        if token not in {"0", "1", "2", "3", "4"}:
            raise ValueError("Unknown goal choice.")
        return cls(int(token), False)

    @property
    def label(self) -> str:
        return "5+" if self.is_five_plus else str(self.minimum)


def settle_score(
    fixture: Fixture,
    home: GoalChoice,
    away: GoalChoice,
    outcome_override: Market | None = None,
) -> BetResult:
    """Derive every supported market from a compact score selection.

    ``5+`` is a lower bound.  It is sufficient for a 2.5 total and for the
    winner whenever the other side is below five.  If both sides are ``5+``,
    the caller must ask for the match outcome explicitly.
    """
    if outcome_override is not None and outcome_override not in {Market.P1, Market.X, Market.P2}:
        raise ValueError("Outcome override must be P1, X or P2.")
    if home.is_five_plus and away.is_five_plus:
        outcome = outcome_override
    elif home.minimum > away.minimum:
        outcome = Market.P1
    elif home.minimum < away.minimum:
        outcome = Market.P2
    else:
        outcome = Market.X
    if outcome is None:
        raise ValueError("Outcome is ambiguous for 5+ — 5+.")

    min_total = Decimal(home.minimum + away.minimum)
    total_is_exact = not home.is_five_plus and not away.is_five_plus
    if min_total > fixture.total_line:
        total_winners, total_returns = {Market.TB}, set()
    elif total_is_exact and min_total < fixture.total_line:
        total_winners, total_returns = {Market.TM}, set()
    elif total_is_exact and min_total == fixture.total_line:
        total_winners, total_returns = set(), {Market.TB, Market.TM}
    else:
        raise ValueError("Total is ambiguous for this line and a 5+ score.")

    outcome_winners = {
        Market.P1: {Market.P1, Market.ONE_X},
        Market.X: {Market.X, Market.ONE_X, Market.X_TWO},
        Market.P2: {Market.P2, Market.X_TWO},
    }[outcome]
    return BetResult(
        fixture.match_id,
        frozenset(outcome_winners | total_winners),
        frozenset(total_returns),
        f"{home.label}:{away.label}",
    )
