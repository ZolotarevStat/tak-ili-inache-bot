from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import BetResult, LeaderboardEntry, PartialLeaderboardEntry, Prediction, ScoredBet


def score_partial_predictions(
    predictions: list[Prediction], results: list[BetResult]
) -> list[PartialLeaderboardEntry]:
    """Score only bets whose outcome is already known.

    A bet is settled when every leg has a result, or as soon as one completed
    leg loses. Missing results never turn a still-live bet into a loss.
    """
    result_by_match = {result.match_id: result for result in results}
    totals: dict[str, tuple[int, int, int]] = {}
    for prediction in predictions:
        realized, settled = 0, 0
        for bet in prediction.bets:
            combined_odds = Decimal("1.00")
            pending = False
            lost = False
            for event in bet.events:
                result = result_by_match.get(event.match_id)
                if result is None:
                    pending = True
                    continue
                if event.market in result.returned_markets:
                    continue
                combined_odds *= event.odds_snapshot
                if event.market not in result.winning_markets:
                    lost = True
            if lost:
                settled += 1
            elif not pending:
                settled += 1
                realized += int(
                    (Decimal(bet.stake) * combined_odds).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
        totals[prediction.participant_id] = (realized, settled, len(prediction.bets) - settled)

    leaderboard: list[PartialLeaderboardEntry] = []
    previous_total: int | None = None
    rank = 0
    ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
    for position, (participant_id, (realized, settled, pending)) in enumerate(ordered, 1):
        if realized != previous_total:
            rank = position
            previous_total = realized
        leaderboard.append(PartialLeaderboardEntry(rank, participant_id, realized, settled, pending))
    return leaderboard


def score_predictions(predictions: list[Prediction], results: list[BetResult]) -> tuple[list[ScoredBet], list[LeaderboardEntry]]:
    result_by_match = {result.match_id: result for result in results}
    scored: list[ScoredBet] = []
    totals: dict[str, tuple[int, int]] = {}
    for prediction in predictions:
        gross, won = 0, 0
        for bet_no, bet in enumerate(prediction.bets, 1):
            combined_odds = Decimal("1.00")
            is_win = True
            for event in bet.events:
                result = result_by_match.get(event.match_id)
                if result is None or event.market not in result.returned_markets:
                    combined_odds *= event.odds_snapshot
                if result is None or (event.market not in result.winning_markets and event.market not in result.returned_markets):
                    is_win = False
            payout = int((Decimal(bet.stake) * combined_odds).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if is_win else 0
            scored.append(ScoredBet(prediction.participant_id, bet_no, bet.bet_type, bet.stake, combined_odds, is_win, payout))
            gross += payout
            won += int(is_win)
        totals[prediction.participant_id] = (gross, won)
    leaderboard: list[LeaderboardEntry] = []
    previous_total: int | None = None
    rank = 0
    for position, (participant_id, (gross, won)) in enumerate(sorted(totals.items(), key=lambda item: (-item[1][0], item[0])), 1):
        if gross != previous_total:
            rank = position
            previous_total = gross
        leaderboard.append(LeaderboardEntry(rank, participant_id, gross, gross - 5_000, won))
    return scored, leaderboard
