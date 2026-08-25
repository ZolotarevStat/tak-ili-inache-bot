from __future__ import annotations

import socket
import unittest

from tak_ili_inache.transport import FamilyConnector, TransportConfig
from tak_ili_inache.transport_canary import run


class _Socket:
    def __init__(self, fail): self.fail = fail
    def settimeout(self, _value): pass
    def connect(self, _address):
        if self.fail: raise TimeoutError("test-only tcp timeout")
    def close(self): pass


class _Context:
    def wrap_socket(self, sock, server_hostname):
        self.server_hostname = server_hostname
        return sock


class _Response:
    status = 404
    def read(self): return b""


class _Connection:
    def __init__(self, *_args, **_kwargs): self.sock = None
    def request(self, *_args, **_kwargs): pass
    def getresponse(self): return _Response()


class TransportCanaryTests(unittest.TestCase):
    def _connector(self, fail_predicate):
        attempts = [0]
        def factory(*_args):
            attempts[0] += 1
            return _Socket(fail_predicate(attempts[0]))
        context = _Context()
        connector = FamilyConnector(
            TransportConfig("ipv6", connect_timeout=0.6, pre_send_attempts=3),
            resolver=lambda *_args: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("v6", 443, 0, 0))],
            socket_factory=factory,
            context=context,
            sleep=lambda _delay: None,
        )
        return connector, context

    def test_observed_first_attempt_timeout_rate_recovers_all_200_logical_calls(self):
        # Every tenth logical call loses its first SYN; retry re-resolves before HTTP bytes.
        connector, context = self._connector(lambda attempt: (attempt - 1) % 11 == 0)
        report = run(200, connector, _Connection)
        self.assertTrue(report["ok"])
        self.assertEqual((report["logical_calls"], report["successes"], report["failures"]), (200, 200, 0))
        self.assertGreater(report["attempt_failures"], 0)
        self.assertGreater(report["recoveries"], 0)
        self.assertEqual(report["family"], "ipv6")
        self.assertEqual(context.server_hostname, "api.telegram.org")

    def test_repeated_outage_fails_closed(self):
        connector, _context = self._connector(lambda _attempt: True)
        report = run(200, connector, _Connection)
        self.assertFalse(report["ok"])
        self.assertEqual(report["successes"], 0)
        self.assertEqual(report["failures"], 200)
        self.assertEqual(report["family"], "unknown")
        self.assertEqual(report["phases"], {"connect": 200})
