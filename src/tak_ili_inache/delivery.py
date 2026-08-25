from __future__ import annotations

import time


class UnknownDeliveryError(RuntimeError): pass


class DeliveryPolicy:
    """Retry only unequivocal pre-send/429/5xx failures; never replay unknown delivery."""
    def __init__(self, liveness=None, sleep=time.sleep) -> None:
        self.liveness, self.sleep = liveness, sleep

    def send(self, operation, attempts: int = 2):
        for attempt in range(attempts):
            try:
                result = operation()
                if self.liveness: self.liveness.reply_delivered()
                return result
            except UnknownDeliveryError:
                if self.liveness: self.liveness.reply_error()
                raise
            except Exception as error:
                retryable = not getattr(error, "pre_send_exhausted", False) and (getattr(error, "status", None) == 429 or 500 <= getattr(error, "status", 0) < 600 or isinstance(error, (OSError, TimeoutError)))
                if not retryable or attempt + 1 == attempts:
                    if self.liveness: self.liveness.reply_error()
                    raise
                self.sleep(0.1 * (attempt + 1))
