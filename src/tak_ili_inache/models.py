from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Market(str, Enum):
    P1 = "П1"
    X = "Х"
    P2 = "П2"
    TB = "ТБ"
    TM = "ТМ"
    ONE_X = "1Х"
    X_TWO = "Х2"


class BetType(str, Enum):
    SINGLE = "single"
    EXPRESS = "express"


@dataclass(frozen=True)
class Fixture:
    round_id: str
    match_id: str
    kickoff_msk: datetime
    home_team: str
    away_team: str
    total_line: Decimal
    odds: dict[Market, Decimal]


@dataclass(frozen=True)
class Round:
    round_id: str
    fixtures: tuple[Fixture, ...]
    deadline_msk: datetime
    checksum: str


@dataclass(frozen=True)
class BetEvent:
    match_id: str
    market: Market
    odds_snapshot: Decimal
    total_line_snapshot: Decimal | None = None


@dataclass(frozen=True)
class Bet:
    bet_type: BetType
    stake: int
    events: tuple[BetEvent, ...]


@dataclass(frozen=True)
class Prediction:
    round_id: str
    participant_id: str
    bets: tuple[Bet, ...]
    submitted_at_msk: datetime


@dataclass(frozen=True)
class BetResult:
    match_id: str
    winning_markets: frozenset[Market] = frozenset()
    returned_markets: frozenset[Market] = frozenset()
    # Human-readable score entered by an administrator.  Settlement still uses
    # canonical markets; this field exists solely so the result picker can
    # show exactly what has already been entered.
    score_label: str = ""


@dataclass(frozen=True)
class ScoredBet:
    participant_id: str
    bet_no: int
    bet_type: BetType
    stake: int
    combined_odds: Decimal
    is_win: bool
    gross_payout: int


@dataclass(frozen=True)
class LeaderboardEntry:
    rank: int
    participant_id: str
    gross_payout: int
    net_result: int
    winning_bets: int


@dataclass(frozen=True)
class PartialLeaderboardEntry:
    rank: int
    participant_id: str
    realized_payout: int
    settled_bets: int
    pending_bets: int
    maximum_payout: int = 0
    winning_bets: int = 0
    losing_bets: int = 0
    returned_bets: int = 0


@dataclass(frozen=True)
class PartialScoredBet:
    participant_id: str
    bet_no: int
    bet_type: BetType
    stake: int
    status: str
    combined_odds: Decimal
    realized_payout: int
    maximum_payout: int
    event_statuses: tuple[str, ...]


@dataclass(frozen=True)
class Participant:
    participant_id: str
    telegram_id: str
    display_name: str


@dataclass(frozen=True)
class AdminGrant:
    """Current active runtime grant; audit history stays in ``admin_grants.csv``."""
    telegram_id: str
    participant_id: str
    granted_by: str
    granted_at: str
    revision: str
