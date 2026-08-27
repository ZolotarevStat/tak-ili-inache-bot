from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import BetResult, LeaderboardEntry, PartialLeaderboardEntry, PartialScoredBet, Prediction, ScoredBet


def score_partial_bets(
    predictions: list[Prediction], results: list[BetResult]
) -> tuple[list[PartialScoredBet], list[PartialLeaderboardEntry]]:
    """Score settled bets and expose the ceiling of every still-live bet.

    ``maximum_payout`` is a gross, not guaranteed, payout: for a pending bet
    it assumes that every unresolved leg wins. Returned legs have odds 1.00.
    """
    result_by_match = {result.match_id: result for result in results}
    scored: list[PartialScoredBet] = []
    totals: dict[str, dict[str, int]] = {}
    for prediction in predictions:
        summary = {
            "realized": 0, "maximum": 0, "settled": 0, "pending": 0,
            "won": 0, "lost": 0, "returned": 0,
        }
        for bet_no, bet in enumerate(prediction.bets, 1):
            combined_odds = Decimal("1.00")
            event_statuses: list[str] = []
            for event in bet.events:
                result = result_by_match.get(event.match_id)
                if result is None:
                    event_statuses.append("pending")
                    combined_odds *= event.odds_snapshot
                elif event.market in result.returned_markets:
                    event_statuses.append("returned")
                elif event.market in result.winning_markets:
                    event_statuses.append("won")
                    combined_odds *= event.odds_snapshot
                else:
                    event_statuses.append("lost")
                    combined_odds *= event.odds_snapshot

            projected = int(
                (Decimal(bet.stake) * combined_odds).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            if "lost" in event_statuses:
                status, realized, maximum = "lost", 0, 0
                summary["settled"] += 1
                summary["lost"] += 1
            elif "pending" in event_statuses:
                status, realized, maximum = "pending", 0, projected
                summary["pending"] += 1
            elif event_statuses and all(item == "returned" for item in event_statuses):
                status, realized, maximum = "returned", bet.stake, bet.stake
                summary["settled"] += 1
                summary["returned"] += 1
            else:
                status, realized, maximum = "won", projected, projected
                summary["settled"] += 1
                summary["won"] += 1
            summary["realized"] += realized
            summary["maximum"] += maximum
            scored.append(
                PartialScoredBet(
                    prediction.participant_id, bet_no, bet.bet_type, bet.stake,
                    status, combined_odds, realized, maximum, tuple(event_statuses),
                )
            )
        totals[prediction.participant_id] = summary

    leaderboard: list[PartialLeaderboardEntry] = []
    previous_total: int | None = None
    rank = 0
    ordered = sorted(totals.items(), key=lambda item: (-item[1]["realized"], item[0]))
    for position, (participant_id, summary) in enumerate(ordered, 1):
        if summary["realized"] != previous_total:
            rank = position
            previous_total = summary["realized"]
        leaderboard.append(
            PartialLeaderboardEntry(
                rank, participant_id, summary["realized"], summary["settled"], summary["pending"],
                summary["maximum"], summary["won"], summary["lost"], summary["returned"],
            )
        )
    return scored, leaderboard


def score_partial_predictions(
    predictions: list[Prediction], results: list[BetResult]
) -> list[PartialLeaderboardEntry]:
    """Score only bets whose outcome is already known.

    A bet is settled when every leg has a result, or as soon as one completed
    leg loses. Missing results never turn a still-live bet into a loss.
    """
    return score_partial_bets(predictions, results)[1]


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
