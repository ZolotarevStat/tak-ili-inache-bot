"""Product-notification identifiers and copy, separate from infrastructure alerts."""
from __future__ import annotations

import hashlib

from .models import Round

ROUND_OPENED = "round_opened"
PREDICTIONS_PUBLISHED = "predictions_published"
RESULTS_READY = "results_ready"
EVENTS = frozenset({ROUND_OPENED, PREDICTIONS_PUBLISHED, RESULTS_READY})


def recipient_fingerprint(telegram_id: str) -> str:
    return hashlib.sha256(telegram_id.encode("utf-8")).hexdigest()[:24]


def notification_key(event: str, round_id: str, revision: str, recipient: str) -> str:
    if event not in EVENTS:
        raise ValueError("Unknown product notification event.")
    digest = hashlib.sha256(f"{event}\x1f{round_id}\x1f{revision}\x1f{recipient}".encode("utf-8")).hexdigest()[:32]
    return f"product:{event}:{digest}"


def notification_text(event: str, round_: Round) -> str:
    if event == ROUND_OPENED:
        return f"🏟️ Открыт тур {round_.round_id}. Дедлайн: {round_.deadline_msk:%d.%m %H:%M} МСК. Соберите прогноз: /predict"
    if event == PREDICTIONS_PUBLISHED:
        return f"🔒 Прогнозы тура {round_.round_id} опубликованы в турнирном чате."
    if event == RESULTS_READY:
        return f"📊 Результаты тура {round_.round_id} рассчитаны. Рейтинг опубликован в турнирном чате."
    raise ValueError("Unknown product notification event.")
