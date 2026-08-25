"""Pure domain core; intentionally contains no Telegram or infrastructure code."""

from .fixtures import import_fixtures
from .models import Bet, BetEvent, BetResult, Fixture, Prediction, Round
from .scoring import score_predictions
from .validators import ValidationError, validate_prediction

__all__ = [
    "Bet", "BetEvent", "BetResult", "Fixture", "Prediction", "Round",
    "ValidationError", "import_fixtures", "score_predictions", "validate_prediction",
]
