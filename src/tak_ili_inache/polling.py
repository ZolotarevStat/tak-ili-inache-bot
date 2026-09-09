from __future__ import annotations

import logging
import os
import time
import asyncio
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .bot import BotService
from .csv_repository import CsvRepository
from .liveness import LivenessStore
from .delivery import DeliveryPolicy
from .draft_store import DraftStore
from .transport import validate_address_family
from .telegram_api import TelegramApi, TelegramClient
from .async_runtime import AsyncUpdateRuntime


class PollingRunner:
    """One long-poll iteration with an explicit at-most-once skip policy.

    Once a received update has a valid ``update_id``, its offset is advanced even
    when the handler fails. This deliberately drops that update rather than
    replaying a possibly non-idempotent partial handler on every poll.
    """

    def __init__(self, telegram: TelegramClient, handle_update: Callable[[dict], None], liveness: LivenessStore, logger=None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.telegram, self.handle_update, self.liveness = telegram, handle_update, liveness
        self.logger, self.sleep, self.offset = logger or logging.getLogger(__name__), sleep, None

    def poll_once(self) -> int | None:
        try:
            updates = self.telegram.get_updates(self.offset, timeout=30)
        except Exception:
            self.liveness.polling_error()
            self.logger.warning("polling_fail kind=get_updates")
            return self.offset
        for update in updates:
            update_id = _update_id(update)
            if update_id is None:
                self.liveness.handler_error()
                self.logger.warning("handler_fail kind=missing_update_id")
                continue
            try:
                self.handle_update(update)
            except Exception:
                self.liveness.handler_error()
                self.logger.warning("handler_fail kind=update_skipped")
            else:
                self.liveness.successful_update()
            finally:
                self.offset = max(self.offset or update_id + 1, update_id + 1)
        self.liveness.successful_poll()
        return self.offset

    def run_forever(self) -> None:
        self.liveness.start()
        while True:
            before = self.offset
            self.poll_once()
            if self.offset == before:
                self.sleep(2)


def _update_id(update: object) -> int | None:
    if not isinstance(update, dict):
        return None
    value = update.get("update_id")
    return value if isinstance(value, int) and value >= 0 else None


def main() -> None:
    configure_logging()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    data_dir = os.environ.get("TAK_ILI_INACHE_DATA_DIR", "data/runtime")
    admin_ids = {item.strip() for item in os.environ.get("TAK_ILI_INACHE_ADMIN_IDS", "").split(",") if item.strip()}
    chat_id = os.environ.get("TOURNAMENT_CHAT_ID")
    liveness = LivenessStore(data_dir)
    service = BotService(CsvRepository(data_dir), build_telegram(token, liveness, os.environ), lambda: datetime.now(ZoneInfo("Europe/Moscow")), admin_ids=admin_ids, tournament_chat_id=int(chat_id) if chat_id else None, output_dir=os.path.join(data_dir, "output"), draft_store=DraftStore(data_dir))
    asyncio.run(AsyncUpdateRuntime(service.telegram.get_updates, service.handle_update, liveness=liveness, scheduled=service.process_scheduled_notifications).run())


def configure_logging() -> None:
    """Emit only application INFO/WARN records to systemd stderr."""
    logger = logging.getLogger("tak_ili_inache")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


def telegram_address_family(environment: dict[str, str]) -> str:
    """IPv6 is the safe release default until Telegram's IPv4 route is repaired."""
    return validate_address_family(environment.get("TAK_ILI_INACHE_TELEGRAM_ADDRESS_FAMILY", "ipv6").strip().lower())


def build_telegram(token: str, liveness: LivenessStore, environment: dict[str, str]) -> TelegramApi:
    return TelegramApi(token, address_family=telegram_address_family(environment), delivery_policy=DeliveryPolicy(liveness=liveness))


if __name__ == "__main__":
    main()
