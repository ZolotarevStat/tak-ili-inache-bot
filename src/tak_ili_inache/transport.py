from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from time import sleep as blocking_sleep


@dataclass(frozen=True)
class TransportConfig:
    address_family: str = "auto"
    connect_timeout: float = 0.6
    read_timeout: float = 20.0
    pre_send_attempts: int = 3
    retry_delays: tuple[float, ...] = (0.05, 0.10)

    def __post_init__(self) -> None:
        validate_address_family(self.address_family)
        if not 0 < self.connect_timeout <= 0.75:
            raise ValueError("connect timeout must be between 0 and 0.75 seconds")
        if self.pre_send_attempts < 1 or len(self.retry_delays) < self.pre_send_attempts - 1:
            raise ValueError("invalid bounded pre-send retry configuration")

    @property
    def family(self) -> int:
        return ADDRESS_FAMILIES[self.address_family]


class FamilyConnector:
    """Resolve on every request and preserve hostname SNI; never pins Telegram IPs."""

    def __init__(self, config: TransportConfig, resolver=socket.getaddrinfo, socket_factory=socket.socket, context=None, sleep=blocking_sleep) -> None:
        self.config, self.resolver, self.socket_factory = config, resolver, socket_factory
        self.context = context or ssl.create_default_context()
        self.sleep = sleep

    def connect(self, host: str, port: int = 443):
        errors = []
        for family, socktype, proto, _canon, address in self.resolver(host, port, self.config.family, socket.SOCK_STREAM):
            sock = self.socket_factory(family, socktype, proto)
            try:
                sock.settimeout(self.config.connect_timeout)
                sock.connect(address)
                wrapped = self.context.wrap_socket(sock, server_hostname=host)
                wrapped.settimeout(self.config.read_timeout)
                return wrapped, "ipv6" if family == socket.AF_INET6 else "ipv4"
            except Exception as error:
                errors.append(error)
                sock.close()
        raise errors[-1] if errors else OSError("DNS resolution returned no addresses")

    def connect_pre_send(self, host: str, port: int = 443) -> "PreSendConnection":
        """Retry DNS/connect/TLS only before an HTTP request can be written."""
        failures: list[AttemptFailure] = []
        for attempt in range(1, self.config.pre_send_attempts + 1):
            try:
                sock, family = self.connect(host, port)
                return PreSendConnection(sock, family, attempt, tuple(failures))
            except Exception as error:
                failures.append(AttemptFailure(attempt, _phase(error), type(error).__name__))
                if attempt == self.config.pre_send_attempts:
                    raise PreSendFailure(failures) from error
                self.sleep(self.config.retry_delays[attempt - 1])
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class AttemptFailure:
    attempt: int
    phase: str
    exception_class: str


@dataclass(frozen=True)
class PreSendConnection:
    sock: object
    family: str
    attempts: int
    failures: tuple[AttemptFailure, ...]


class PreSendFailure(OSError):
    def __init__(self, failures: list[AttemptFailure]) -> None:
        super().__init__("Telegram pre-send connection attempts exhausted")
        self.failures = tuple(failures)
        self.phase = failures[-1].phase if failures else "connect"
        self.pre_send_exhausted = True


def _phase(error: Exception) -> str:
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ssl.SSLError):
        return "tls"
    return "connect"
ADDRESS_FAMILIES = {"auto": socket.AF_UNSPEC, "ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}


def validate_address_family(value: str) -> str:
    if value not in ADDRESS_FAMILIES:
        raise ValueError("TAK_ILI_INACHE_TELEGRAM_ADDRESS_FAMILY must be ipv6, ipv4 or auto")
    return value
