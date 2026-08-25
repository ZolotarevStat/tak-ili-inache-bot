from __future__ import annotations

import tempfile
import unittest
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from tak_ili_inache.liveness import LivenessStore
from tak_ili_inache.polling import PollingRunner, build_telegram, configure_logging, telegram_address_family
from tak_ili_inache.transport import TransportConfig
import socket


class _Telegram:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.offsets = []

    def get_updates(self, offset, timeout):
        self.offsets.append((offset, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class PollingRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = [datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)]
        self.directory = tempfile.TemporaryDirectory()
        self.liveness = LivenessStore(self.directory.name, now=lambda: self.clock[0])
        self.liveness.start()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_handler_exception_is_skipped_and_next_update_is_processed_once(self) -> None:
        secret_payload = "message text and document must never reach logs"
        telegram = _Telegram([[
            {"update_id": 10, "message": {"text": secret_payload}},
            {"update_id": 11, "message": {"text": "next"}},
        ], []])
        processed, logger = [], Mock()

        def handler(update):
            if update["update_id"] == 10:
                raise RuntimeError(secret_payload)
            processed.append(update["update_id"])

        runner = PollingRunner(telegram, handler, self.liveness, logger=logger)
        self.assertEqual(runner.poll_once(), 12)
        self.assertEqual(runner.poll_once(), 12)
        self.assertEqual(processed, [11])
        self.assertEqual(telegram.offsets, [(None, 30), (12, 30)])
        self.assertEqual(self.liveness.snapshot()["handler_error_count"], 1)
        self.assertNotIn(secret_payload, repr(logger.warning.call_args_list))

    def test_handler_exception_before_any_outbound_call_advances_offset(self) -> None:
        telegram = _Telegram([[{"update_id": 20}]])
        outbound = Mock()
        runner = PollingRunner(telegram, lambda update: (_ for _ in ()).throw(RuntimeError("handler error")), self.liveness, logger=Mock())
        self.assertEqual(runner.poll_once(), 21)
        outbound.assert_not_called()
        self.assertEqual(self.liveness.snapshot()["handler_error_count"], 1)

    def test_get_updates_failure_keeps_offset_and_recovers_on_next_iteration(self) -> None:
        telegram = _Telegram([OSError("transport"), [{"update_id": 30}]])
        processed = []
        runner = PollingRunner(telegram, lambda update: processed.append(update["update_id"]), self.liveness, logger=Mock())
        self.assertIsNone(runner.poll_once())
        self.assertEqual(runner.poll_once(), 31)
        self.assertEqual(processed, [30])
        state = self.liveness.snapshot()
        self.assertEqual(state["polling_error_count"], 1)
        self.assertTrue(state["ok"])

    def test_liveness_becomes_stale_without_successful_poll(self) -> None:
        self.liveness.successful_poll()
        self.clock[0] += timedelta(seconds=91)
        self.assertFalse(self.liveness.snapshot()["ok"])

    def test_release_runtime_defaults_to_ipv6_and_validates_an_override(self) -> None:
        self.assertEqual(telegram_address_family({}), "ipv6")
        self.assertEqual(TransportConfig(telegram_address_family({})).family, socket.AF_INET6)
        self.assertEqual(build_telegram("test-token", self.liveness, {}).connector.config.family, socket.AF_INET6)
        self.assertEqual(telegram_address_family({"TAK_ILI_INACHE_TELEGRAM_ADDRESS_FAMILY": "auto"}), "auto")
        with self.assertRaises(ValueError):
            telegram_address_family({"TAK_ILI_INACHE_TELEGRAM_ADDRESS_FAMILY": "invalid"})

    def test_scoped_transport_logger_emits_info_without_root_debug(self) -> None:
        logger = logging.getLogger("tak_ili_inache")
        prior = list(logger.handlers)
        prior_level, prior_propagate = logger.level, logger.propagate
        for handler in prior: logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        try:
            configure_logging()
            self.assertEqual(logger.level, logging.INFO)
            self.assertEqual(len(logger.handlers), 1)
            self.assertFalse(logger.propagate)
            self.assertIn("%(message)s", logger.handlers[0].formatter._fmt)
        finally:
            for handler in list(logger.handlers): logger.removeHandler(handler)
            for handler in prior: logger.addHandler(handler)
            logger.setLevel(prior_level)
            logger.propagate = prior_propagate
