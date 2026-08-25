"""Tokenless aggregate canary for the runtime DNS/SNI IPv6 connector."""
from __future__ import annotations

import argparse
import http.client
import json
import socket
import time
from dataclasses import dataclass
from typing import Callable

from .transport import FamilyConnector, PreSendFailure, TransportConfig


@dataclass(frozen=True)
class Probe:
    success: bool
    duration_ms: int
    family: str
    phase: str
    attempts: int
    attempt_failures: int
    recovered: bool


def probe(connector: FamilyConnector, connection_factory=http.client.HTTPSConnection, clock=time.monotonic) -> Probe:
    started = clock()
    family, attempts, recovered = "unknown", 0, False
    try:
        result = connector.connect_pre_send("api.telegram.org")
        family, attempts, recovered = result.family, result.attempts, bool(result.failures)
        connection = connection_factory("api.telegram.org", timeout=connector.config.read_timeout)
        connection.sock = result.sock
        # Tokenless endpoint: only validates a complete HTTPS/HTTP round-trip.
        connection.request("GET", "/", headers={"Host": "api.telegram.org"})
        response = connection.getresponse()
        response.read()
        return Probe(True, _elapsed(started, clock), family, "none", attempts, len(result.failures), recovered)
    except PreSendFailure as error:
        return Probe(False, _elapsed(started, clock), family, error.phase, len(error.failures), len(error.failures), False)
    except Exception:
        return Probe(False, _elapsed(started, clock), family, "http_or_read", attempts, max(0, attempts - 1), recovered)


def run(calls: int, connector: FamilyConnector, connection_factory=http.client.HTTPSConnection, clock=time.monotonic) -> dict[str, object]:
    probes = [probe(connector, connection_factory, clock) for _ in range(calls)]
    durations = sorted(item.duration_ms for item in probes)
    families = sorted({item.family for item in probes})
    final_failures = sum(not item.success for item in probes)
    report = {
        "logical_calls": calls,
        "successes": calls - final_failures,
        "failures": final_failures,
        "attempt_failures": sum(item.attempt_failures for item in probes),
        "recoveries": sum(item.recovered for item in probes),
        "family": families[0] if len(families) == 1 else "mixed",
        "phases": {phase: sum(item.phase == phase for item in probes) for phase in sorted({item.phase for item in probes})},
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "p99_ms": _percentile(durations, 99),
        "max_ms": max(durations, default=0),
    }
    report["ok"] = final_failures == 0 and report["family"] == "ipv6" and report["p95_ms"] <= 2000 and report["p99_ms"] <= 5000
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Tokenless DNS/SNI IPv6 transport canary")
    parser.add_argument("--calls", type=int, default=200)
    args = parser.parse_args()
    if args.calls < 1 or args.calls > 1000:
        parser.error("--calls must be between 1 and 1000")
    connector = FamilyConnector(TransportConfig(address_family="ipv6"))
    report = run(args.calls, connector)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


def _elapsed(started: float, clock: Callable[[], float]) -> int:
    return max(0, int((clock() - started) * 1000))


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    index = max(0, (len(values) * percentile + 99) // 100 - 1)
    return values[index]
