"""Read non-sensitive Telegram IDs from pending updates before polling starts."""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from .telegram_api import TelegramApi


def extract_message_ids(updates: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Extract only message sender/chat IDs; does not acknowledge or mutate updates."""
    found: set[tuple[int, int]] = set()
    for update in updates:
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        sender = message.get("from", {}).get("id")
        chat = message.get("chat", {}).get("id")
        if isinstance(sender, int) and isinstance(chat, int):
            found.add((sender, chat))
    return [{"message_from_id": sender, "message_chat_id": chat} for sender, chat in sorted(found)]


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not configured.", file=sys.stderr)
        raise SystemExit(2)
    try:
        # getUpdates without an offset is read-only: it neither confirms nor removes updates.
        updates = TelegramApi(token).get_updates(offset=None, timeout=0)
        print(json.dumps(extract_message_ids(updates), ensure_ascii=False))
    except Exception:
        # Do not expose token-bearing endpoint details from transport exceptions.
        print("Could not read Telegram updates; check network and protected environment configuration.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
