from __future__ import annotations

import socket
import unittest

from tak_ili_inache.transport import FamilyConnector, TransportConfig


class _Socket:
    def __init__(self, family, fail): self.family, self.fail = family, fail
    def settimeout(self, value): pass
    def connect(self, address):
        if self.fail: raise TimeoutError("connect")
    def close(self): pass


class _Context:
    def wrap_socket(self, sock, server_hostname):
        self.server_name = server_hostname
        return sock


class TransportTests(unittest.TestCase):
    def test_ipv4_timeout_falls_back_to_dns_resolved_ipv6_with_sni(self):
        context = _Context()
        connector = FamilyConnector(
            TransportConfig("auto"),
            resolver=lambda host, port, family, kind: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("v4", 443)), (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("v6", 443, 0, 0))],
            socket_factory=lambda family, *_: _Socket(family, family == socket.AF_INET), context=context,
        )
        _socket, family = connector.connect("api.telegram.org")
        self.assertEqual(family, "ipv6")
        self.assertEqual(context.server_name, "api.telegram.org")

    def test_ipv6_mode_never_requests_ipv4_addresses(self):
        requested = []
        connector = FamilyConnector(TransportConfig("ipv6"), resolver=lambda host, port, family, kind: requested.append(family) or [], context=_Context())
        with self.assertRaises(OSError): connector.connect("api.telegram.org")
        self.assertEqual(requested, [socket.AF_INET6])
