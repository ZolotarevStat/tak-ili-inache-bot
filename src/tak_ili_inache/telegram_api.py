from __future__ import annotations

import http.client
import json
import logging
import mimetypes
import socket
import ssl
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .delivery import DeliveryPolicy, UnknownDeliveryError
from .transport import FamilyConnector, PreSendFailure, TransportConfig

_delivery_attempt: ContextVar[int] = ContextVar("telegram_delivery_attempt", default=1)


class TelegramClient(Protocol):
    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> int | None: ...
    def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def clear_keyboard(self, chat_id: int, message_id: int) -> None: ...
    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]: ...
    def download_document(self, document: dict[str, Any]) -> bytes: ...
    def send_document(self, chat_id: int, path: str, caption: str = "", parse_mode: str | None = None) -> None: ...
    def send_photo(self, chat_id: int, path: str, caption: str = "") -> None: ...
    def send_photo_bytes(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None: ...
    def send_media_group_bytes(self, chat_id: int, media: Sequence[tuple[str, bytes]], caption: str = "") -> None: ...


class TelegramHttpError(RuntimeError):
    """Safe HTTP error metadata used only for retry classification."""

    def __init__(self, status: int, kind: str | None = None) -> None:
        super().__init__("Telegram HTTP failure")
        self.status = status
        self.kind = kind


class TelegramApi:
    """Bot API wrapper with DNS-per-request and hostname-preserving TLS.

    The token is interpolated only into an in-memory request path and is never logged.
    """

    host = "api.telegram.org"

    def __init__(
        self,
        token: str,
        address_family: str = "auto",
        connector=None,
        delivery_policy: DeliveryPolicy | None = None,
        connection_factory: Callable[..., http.client.HTTPSConnection] = http.client.HTTPSConnection,
    ) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required.")
        self._token = token
        self.connector = connector or FamilyConnector(TransportConfig(address_family))
        self.delivery_policy = delivery_policy or DeliveryPolicy()
        self.connection_factory = connection_factory

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> int | None:
        result = self._deliver(lambda: self._call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}))
        payload = result.get("result") if isinstance(result, dict) else None
        message_id = payload.get("message_id") if isinstance(payload, dict) else None
        return message_id if isinstance(message_id, int) else None

    def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        def edit() -> dict[str, Any]:
            try:
                return self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
            except TelegramHttpError as error:
                if error.kind == "message_not_modified":
                    return {"ok": True, "result": True}
                raise

        self._deliver(edit)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text or None})

    def clear_keyboard(self, chat_id: int, message_id: int) -> None:
        self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": None})

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        response = self._call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}, timeout + 10)
        return response["result"]

    def download_document(self, document: dict[str, Any]) -> bytes:
        file_path = self._call("getFile", {"file_id": document["file_id"]})["result"]["file_path"]
        return self._bytes_request(f"/file/bot{self._token}/{file_path}", 30, "downloadFile")

    def send_document(self, chat_id: int, path: str, caption: str = "", parse_mode: str | None = None) -> None:
        self._deliver(lambda: self._upload("sendDocument", "document", chat_id, path, caption, parse_mode=parse_mode))

    def send_photo(self, chat_id: int, path: str, caption: str = "") -> None:
        self._deliver(lambda: self._upload("sendPhoto", "photo", chat_id, path, caption))

    def send_photo_bytes(self, chat_id: int, filename: str, content: bytes, caption: str = "") -> None:
        """Upload a rendered image without creating a filesystem artifact."""
        self._deliver(lambda: self._upload_bytes("sendPhoto", "photo", chat_id, filename, content, caption))

    def send_media_group_bytes(
        self,
        chat_id: int,
        media: Sequence[tuple[str, bytes]],
        caption: str = "",
    ) -> None:
        """Upload one Telegram photo album without filesystem artifacts."""
        if not 2 <= len(media) <= 10:
            raise ValueError("Telegram media groups require between 2 and 10 items.")
        self._deliver(lambda: self._upload_media_group_bytes(chat_id, media, caption))

    def _deliver(self, operation: Callable[[], Any]) -> Any:
        attempt = 0

        def invoke() -> Any:
            nonlocal attempt
            attempt += 1
            marker = _delivery_attempt.set(attempt)
            try:
                return operation()
            finally:
                _delivery_attempt.reset(marker)

        return self.delivery_policy.send(invoke)

    def _call(self, method: str, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
        body = json.dumps({key: value for key, value in payload.items() if value is not None}).encode()
        data = self._json_request(f"/bot{self._token}/{method}", body, {"Content-Type": "application/json"}, timeout, method)
        if not data.get("ok"):
            raise TelegramHttpError(200, _telegram_error_kind(data))
        return data

    def _upload(
        self, method: str, field: str, chat_id: int, path: str, caption: str, *, parse_mode: str | None = None
    ) -> None:
        source = Path(path)
        self._upload_bytes(method, field, chat_id, source.name, source.read_bytes(), caption, parse_mode=parse_mode)

    def _upload_bytes(
        self,
        method: str,
        field: str,
        chat_id: int,
        filename: str,
        content: bytes,
        caption: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        boundary = uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\nContent-Type: {mimetypes.guess_type(filename)[0] or 'application/octet-stream'}\r\n\r\n".encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        if parse_mode is not None:
            parts.insert(2, f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\n{parse_mode}\r\n".encode())
        data = self._json_request(f"/bot{self._token}/{method}", b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}, 30, method)
        if not data.get("ok"):
            raise TelegramHttpError(200, _telegram_error_kind(data))

    def _upload_media_group_bytes(
        self,
        chat_id: int,
        media: Sequence[tuple[str, bytes]],
        caption: str,
    ) -> None:
        boundary = uuid.uuid4().hex
        descriptors = []
        for index, (_filename, _content) in enumerate(media):
            descriptor = {"type": "photo", "media": f"attach://photo_{index}"}
            if index == 0 and caption:
                descriptor["caption"] = caption
            descriptors.append(descriptor)
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"\r\n\r\n"
                + json.dumps(descriptors, ensure_ascii=False)
                + "\r\n"
            ).encode(),
        ]
        for index, (filename, content) in enumerate(media):
            parts.extend((
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo_{index}\"; "
                    f"filename=\"{filename}\"\r\nContent-Type: "
                    f"{mimetypes.guess_type(filename)[0] or 'application/octet-stream'}\r\n\r\n"
                ).encode(),
                content,
                b"\r\n",
            ))
        parts.append(f"--{boundary}--\r\n".encode())
        data = self._json_request(
            f"/bot{self._token}/sendMediaGroup",
            b"".join(parts),
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            60,
            "sendMediaGroup",
        )
        if not data.get("ok"):
            raise TelegramHttpError(200, _telegram_error_kind(data))

    def _json_request(self, path: str, body: bytes, headers: dict[str, str], timeout: int, method: str) -> dict[str, Any]:
        raw = self._request("POST", path, body, headers, timeout, method)
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as error:
            raise UnknownDeliveryError("Telegram response could not be decoded") from error

    def _bytes_request(self, path: str, timeout: int, method: str) -> bytes:
        return self._request("GET", path, None, {}, timeout, method)

    def _request(self, verb: str, path: str, body: bytes | None, headers: dict[str, str], timeout: int, method: str) -> bytes:
        started = time.monotonic()
        family = "unknown"
        sent = False
        try:
            pre_send = self._connect_pre_send()
            sock, family = pre_send.sock, pre_send.family
            for failure in pre_send.failures:
                self._telemetry(method, _delivery_attempt.get(), started, "pre_send_failure", failure.exception_class, None, failure.phase, family, failure.attempt)
            if pre_send.failures:
                self._telemetry(method, _delivery_attempt.get(), started, "recovered_pre_send", None, None, "none", family, pre_send.attempts)
            connection = self.connection_factory(self.host, timeout=timeout)
            # FamilyConnector keeps a short timeout while connecting/TLS. Once
            # that phase succeeds, the request's own budget governs reads: a
            # 30-second getUpdates long poll therefore receives 40 seconds.
            sock.settimeout(timeout)
            connection.sock = sock
            # http.client can fail after writing a prefix of the request. Treat the
            # whole request call as delivery-unknown instead of risking a duplicate.
            sent = True
            connection.request(verb, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise TelegramHttpError(response.status, _telegram_error_kind(raw))
            self._telemetry(method, _delivery_attempt.get(), started, "delivered", None, None, "none", family, pre_send.attempts)
            return raw
        except PreSendFailure as error:
            phase = error.phase
            self._telemetry(method, _delivery_attempt.get(), started, "pre_send_exhausted", type(error).__name__, None, phase, family, len(error.failures))
            raise
        except TelegramHttpError as error:
            outcome = "benign_noop" if method == "editMessageText" and error.kind == "message_not_modified" else "failed"
            self._telemetry(method, _delivery_attempt.get(), started, outcome, type(error).__name__, error.status, "http", family, pre_send.attempts)
            raise
        except UnknownDeliveryError as error:
            self._telemetry(method, _delivery_attempt.get(), started, "unknown", type(error).__name__, None, "read", family, pre_send.attempts)
            raise
        except Exception as error:
            phase = _timeout_phase(error, sent)
            delivery_error: Exception = UnknownDeliveryError("Telegram delivery is unknown") if sent else error
            self._telemetry(method, _delivery_attempt.get(), started, "unknown" if sent else "failed", type(error).__name__, None, phase, family, pre_send.attempts if "pre_send" in locals() else 0)
            raise delivery_error from error

    def _connect_pre_send(self):
        method = getattr(self.connector, "connect_pre_send", None)
        if method:
            return method(self.host)
        sock, family = self.connector.connect(self.host)
        # Compatibility with narrow test doubles. Production connector always
        # supplies retry/re-resolution metadata.
        return type("PreSend", (), {"sock": sock, "family": family, "attempts": 1, "failures": ()})()

    @staticmethod
    def _telemetry(method: str, attempt: int, started: float, outcome: str, exception_class: str | None, status: int | None, timeout_phase: str, family: str, connect_attempt: int = 1) -> None:
        logging.getLogger(__name__).info(
            "telegram_transport method=%s logical_attempt=%s connect_attempt=%s duration_ms=%s outcome=%s exception=%s status=%s timeout_phase=%s family=%s",
            method,
            attempt,
            connect_attempt,
            int((time.monotonic() - started) * 1000),
            outcome,
            exception_class or "none",
            status if status is not None else "none",
            timeout_phase,
            family,
        )


def _timeout_phase(error: Exception, sent: bool) -> str:
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "read" if sent else "connect"
    return "read" if sent else "connect"


def _telegram_error_kind(value: bytes | dict[str, Any]) -> str | None:
    try:
        payload = json.loads(value) if isinstance(value, bytes) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    description = payload.get("description") if isinstance(payload, dict) else None
    return "message_not_modified" if isinstance(description, str) and "message is not modified" in description.lower() else None
