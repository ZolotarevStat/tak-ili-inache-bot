from __future__ import annotations

import asyncio
import unittest

from tak_ili_inache.async_runtime import AsyncUpdateRuntime


class AsyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_chat_does_not_block_another_chat_and_duplicate_is_ignored(self) -> None:
        updates = [[
            {"update_id": 1, "message": {"chat": {"id": 10}}},
            {"update_id": 2, "message": {"chat": {"id": 20}}},
            {"update_id": 2, "message": {"chat": {"id": 20}}},
        ]]
        handled = []

        def get_updates(offset, timeout):
            return updates.pop(0)

        def handle(update):
            chat = update["message"]["chat"]["id"]
            if chat == 10:
                __import__("time").sleep(0.08)
            handled.append(update["update_id"])

        runtime = AsyncUpdateRuntime(get_updates, handle, workers=2)
        self.assertEqual(await runtime.poll_once(), 3)
        first, second = asyncio.create_task(runtime.process_one()), asyncio.create_task(runtime.process_one())
        await asyncio.sleep(0.02)
        self.assertIn(2, handled)
        await asyncio.gather(first, second)
        self.assertEqual(sorted(handled), [1, 2])

    async def test_bounded_queue_applies_backpressure_without_losing_updates(self) -> None:
        def get_updates(offset, timeout):
            return [{"update_id": 1, "message": {"chat": {"id": 1}}}, {"update_id": 2, "message": {"chat": {"id": 2}}}]

        runtime = AsyncUpdateRuntime(get_updates, lambda update: None, queue_size=1)
        poll = asyncio.create_task(runtime.poll_once())
        await asyncio.sleep(0)
        self.assertFalse(poll.done())
        await runtime.process_one()
        self.assertEqual(await poll, 3)
        await runtime.process_one()

    async def test_poll_failures_use_bounded_exponential_backoff_and_success_does_not_sleep(self) -> None:
        responses = iter([OSError("down"), OSError("down"), []])
        sleeps = []

        def get_updates(_offset, _timeout):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        async def sleep(value):
            sleeps.append(value)

        runtime = AsyncUpdateRuntime(get_updates, lambda _: None, sleep=sleep, jitter=lambda value: value)
        await runtime.poll_with_backoff_once()
        await runtime.poll_with_backoff_once()
        await runtime.poll_with_backoff_once()
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(runtime.poll_failure_streak, 0)
