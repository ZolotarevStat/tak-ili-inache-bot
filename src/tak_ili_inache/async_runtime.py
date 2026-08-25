"""Bounded asynchronous dispatcher around the synchronous MVP bot handler."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable


class AsyncUpdateRuntime:
    """Poll in one task; process different chats concurrently but serially per chat."""

    def __init__(self, get_updates: Callable, handle_update: Callable, queue_size: int = 64, workers: int = 4, logger=None, liveness=None, sleep=asyncio.sleep, jitter=lambda delay: delay) -> None:
        self.get_updates, self.handle_update = get_updates, handle_update
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_size)
        self.workers, self.logger = workers, logger or logging.getLogger(__name__)
        self.offset: int | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._seen: deque[int] = deque(maxlen=512)
        self.liveness = liveness
        self.sleep, self.jitter = sleep, jitter
        self.poll_failure_streak = 0
        self.last_poll_failed = False

    async def poll_once(self) -> int | None:
        try:
            updates = await asyncio.to_thread(self.get_updates, self.offset, 30)
        except Exception:
            if self.liveness:
                self.liveness.polling_error()
            self.logger.warning("polling_fail kind=get_updates")
            self.poll_failure_streak += 1
            self.last_poll_failed = True
            return self.offset
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(update_id, int):
                self.logger.warning("polling_fail kind=missing_update_id")
                continue
            if update_id in self._seen:
                self.offset = max(self.offset or update_id + 1, update_id + 1)
                continue
            await self.queue.put(update)  # backpressure: never drop a received update.
            self._seen.append(update_id)
            self.offset = max(self.offset or update_id + 1, update_id + 1)
        if self.liveness:
            self.liveness.successful_poll()
        self.poll_failure_streak = 0
        self.last_poll_failed = False
        return self.offset

    async def poll_with_backoff_once(self) -> int | None:
        offset = await self.poll_once()
        if self.last_poll_failed:
            delay = min(8.0, 0.5 * (2 ** (self.poll_failure_streak - 1)))
            await self.sleep(max(0.0, self.jitter(delay)))
        return offset

    async def process_one(self) -> None:
        update = await self.queue.get()
        try:
            chat_key = _chat_key(update)
            lock = self._locks.setdefault(chat_key, asyncio.Lock())
            async with lock:
                try:
                    await asyncio.to_thread(self.handle_update, update)
                except Exception:
                    if self.liveness:
                        self.liveness.handler_error()
                    self.logger.warning("handler_fail kind=update_skipped")
                else:
                    if self.liveness:
                        self.liveness.successful_update()
        finally:
            self.queue.task_done()

    async def run(self) -> None:
        if self.liveness:
            self.liveness.start()
        workers = [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        try:
            while True:
                await self.poll_with_backoff_once()
        finally:
            for worker in workers:
                worker.cancel()

    async def _worker(self) -> None:
        while True:
            await self.process_one()


def _chat_key(update: dict) -> str:
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    return str(chat_id) if isinstance(chat_id, int) else "unknown"
