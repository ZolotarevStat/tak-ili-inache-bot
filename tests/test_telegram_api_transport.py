from __future__ import annotations

import logging
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tak_ili_inache.delivery import DeliveryPolicy, UnknownDeliveryError
from tak_ili_inache.bot import BotService
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.fixtures import import_fixtures
from tak_ili_inache.telegram_api import TelegramApi, TelegramHttpError
from tak_ili_inache.polling import configure_logging
from tak_ili_inache.transport import AttemptFailure, PreSendConnection, PreSendFailure


class _Connector:
    def __init__(self):
        self.hosts: list[str] = []

    def connect(self, host: str):
        self.hosts.append(host)
        return _Socket(), "ipv6"


class _Socket:
    def __init__(self): self.timeouts = []
    def settimeout(self, value): self.timeouts.append(value)


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status, self.body = status, body

    def read(self) -> bytes:
        return self.body


class _Connection:
    def __init__(self, host, timeout, responses, requests):
        self.host, self.timeout, self.responses, self.requests = host, timeout, responses, requests
        self.sock = None

    def request(self, verb, path, body=None, headers=None):
        self.requests.append((verb, path, body, headers, self.sock))

    def getresponse(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _Factory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, host, timeout):
        return _Connection(host, timeout, self.responses, self.requests)


class _Liveness:
    def __init__(self): self.delivered = self.errors = 0
    def reply_delivered(self): self.delivered += 1
    def reply_error(self): self.errors += 1


class TelegramApiTransportTests(unittest.TestCase):
    def _api(self, responses, liveness=None):
        connector, factory = _Connector(), _Factory(responses)
        api = TelegramApi("123456:secret-token", connector=connector, connection_factory=factory, delivery_policy=DeliveryPolicy(liveness=liveness, sleep=lambda _: None))
        return api, connector, factory

    def test_all_public_api_paths_use_dns_sni_connector_and_normalized_requests(self):
        api, connector, factory = self._api([
            _Response(200, b'{"ok":true,"result":[]}'),
            _Response(200, b'{"ok":true,"result":true}'),
            _Response(200, b'{"ok":true,"result":true}'),
            _Response(200, b'{"ok":true,"result":true}'),
            _Response(200, b'{"ok":true,"result":true}'),
            _Response(200, b'{"ok":true,"result":{"file_path":"docs/a.csv"}}'),
            _Response(200, b"csv-bytes"),
            _Response(200, b'{"ok":true,"result":true}'),
            _Response(200, b'{"ok":true,"result":true}'),
            _Response(200, b'{"ok":true,"result":true}'),
        ])
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "chart.png"
            artifact.write_bytes(b"PNG")
            self.assertEqual(api.get_updates(7, 30), [])
            api.send_message(42, "private text")
            api.answer_callback("callback-id")
            api.clear_keyboard(42, 99)
            api.edit_message(42, 99, "card text")
            self.assertEqual(api.download_document({"file_id": "document-id"}), b"csv-bytes")
            api.send_document(42, str(artifact), "caption")
            api.send_photo(42, str(artifact), "caption")
            api.send_photo_bytes(42, "in-memory.png", b"PNG-BYTES", "caption")
        self.assertEqual(connector.hosts, ["api.telegram.org"] * 10)
        self.assertEqual([request[0] for request in factory.requests], ["POST", "POST", "POST", "POST", "POST", "POST", "GET", "POST", "POST", "POST"])
        paths = [request[1].rsplit("/", 1)[-1] for request in factory.requests]
        self.assertEqual(paths, ["getUpdates", "sendMessage", "answerCallbackQuery", "editMessageReplyMarkup", "editMessageText", "getFile", "a.csv", "sendDocument", "sendPhoto", "sendPhoto"])
        self.assertTrue(all(request[4] is not None for request in factory.requests))
        self.assertIn(b'name="photo"', factory.requests[-1][2])
        self.assertIn(b'filename="in-memory.png"', factory.requests[-1][2])
        self.assertIn(b"Content-Type: image/png", factory.requests[-1][2])
        self.assertIn(b"PNG-BYTES", factory.requests[-1][2])

    def test_photo_bytes_upload_needs_no_local_file(self):
        api, _connector, factory = self._api([_Response(200, b'{"ok":true,"result":true}')])
        image = b"\x89PNG\r\n\x1a\nbytes-only"
        api.send_photo_bytes(42, "rendered.png", image, "")
        body = factory.requests[0][2]
        self.assertIn(b'filename="rendered.png"', body)
        self.assertIn(image, body)

    def test_document_html_parse_mode_is_opt_in_and_encoded_as_multipart_field(self):
        api, _connector, factory = self._api([_Response(200, b'{"ok":true,"result":true}')])
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "report.csv"
            artifact.write_bytes(b"csv")
            api.send_document(42, str(artifact), "<b>caption</b>", parse_mode="HTML")
        body = factory.requests[0][2]
        self.assertIn(b'name="caption"\r\n\r\n<b>caption</b>', body)
        self.assertIn(b'name="parse_mode"\r\n\r\nHTML', body)

    def test_main_replies_retry_safe_http_error_and_update_liveness(self):
        liveness = _Liveness()
        api, _connector, factory = self._api([_Response(503, b"unavailable"), _Response(200, b'{"ok":true,"result":true}')], liveness)
        logger = logging.getLogger("tak_ili_inache.telegram_api")
        with self.assertLogs(logger, "INFO") as captured:
            api.send_message(42, "reply")
        self.assertEqual(len(factory.requests), 2)
        self.assertEqual(liveness.delivered, 1)
        self.assertEqual(liveness.errors, 0)
        self.assertIn("attempt=2", "\n".join(captured.output))

    def test_edit_message_not_modified_is_a_benign_noop(self):
        liveness = _Liveness()
        response = b'{"ok":false,"error_code":400,"description":"Bad Request: message is not modified: specified new message content and reply markup are exactly the same"}'
        api, _connector, factory = self._api([_Response(400, response)], liveness)
        logger = logging.getLogger("tak_ili_inache.telegram_api")
        with self.assertLogs(logger, "INFO") as captured:
            api.edit_message(42, 99, "same card")
        self.assertEqual(len(factory.requests), 1)
        self.assertEqual((liveness.delivered, liveness.errors), (1, 0))
        self.assertIn("outcome=benign_noop", "\n".join(captured.output))

    def test_other_edit_http_400_is_not_hidden(self):
        liveness = _Liveness()
        response = b'{"ok":false,"error_code":400,"description":"Bad Request: message to edit not found"}'
        api, _connector, factory = self._api([_Response(400, response)], liveness)
        with self.assertRaises(TelegramHttpError):
            api.edit_message(42, 99, "card")
        self.assertEqual(len(factory.requests), 1)
        self.assertEqual((liveness.delivered, liveness.errors), (0, 1))

    def test_main_reply_retries_connector_failure_before_request(self):
        class _FlakyConnector(_Connector):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def connect(self, host):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("connect timed out")
                return super().connect(host)

        connector, factory = _FlakyConnector(), _Factory([_Response(200, b'{"ok":true,"result":true}')])
        api = TelegramApi("123456:secret-token", connector=connector, connection_factory=factory, delivery_policy=DeliveryPolicy(sleep=lambda _: None))
        api.send_message(42, "reply")
        self.assertEqual(connector.calls, 2)
        self.assertEqual(len(factory.requests), 1)

    def test_recovered_pre_send_failure_has_attempt_phase_telemetry(self):
        class _RetryingConnector:
            def connect_pre_send(self, _host):
                return PreSendConnection(_Socket(), "ipv6", 2, (AttemptFailure(1, "connect", "TimeoutError"),))

        factory = _Factory([_Response(200, b'{"ok":true,"result":true}')])
        api = TelegramApi("123456:secret-token", connector=_RetryingConnector(), connection_factory=factory, delivery_policy=DeliveryPolicy(sleep=lambda _: None))
        logger = logging.getLogger("tak_ili_inache.telegram_api")
        with self.assertLogs(logger, "INFO") as captured:
            api.send_message(42, "reply")
        records = "\n".join(captured.output)
        self.assertIn("outcome=pre_send_failure", records)
        self.assertIn("outcome=recovered_pre_send", records)
        self.assertIn("logical_attempt=1", records)
        self.assertIn("connect_attempt=2", records)
        self.assertNotIn("secret-token", records)

    def test_exhausted_pre_send_budget_is_not_retried_by_delivery_policy(self):
        class _ExhaustedConnector:
            def __init__(self): self.calls = 0
            def connect_pre_send(self, _host):
                self.calls += 1
                raise PreSendFailure([AttemptFailure(1, "connect", "TimeoutError"), AttemptFailure(2, "connect", "TimeoutError"), AttemptFailure(3, "connect", "TimeoutError")])

        connector = _ExhaustedConnector()
        api = TelegramApi("123456:secret-token", connector=connector, connection_factory=_Factory([]), delivery_policy=DeliveryPolicy(sleep=lambda _: None))
        with self.assertRaises(PreSendFailure):
            api.send_message(42, "reply")
        self.assertEqual(connector.calls, 1)

    def test_unknown_post_send_reply_is_not_replayed_and_is_recorded(self):
        liveness = _Liveness()
        api, _connector, factory = self._api([OSError("read interrupted")], liveness)
        with self.assertRaises(UnknownDeliveryError):
            api.send_message(42, "reply")
        self.assertEqual(len(factory.requests), 1)
        self.assertEqual(liveness.delivered, 0)
        self.assertEqual(liveness.errors, 1)

    def test_telemetry_is_anonymized_and_has_transport_dimensions(self):
        api, _connector, _factory = self._api([_Response(200, b'{"ok":true,"result":true}')])
        logger = logging.getLogger("tak_ili_inache.telegram_api")
        with self.assertLogs(logger, "INFO") as captured:
            api.send_message(987654321, "private text")
        record = "\n".join(captured.output)
        self.assertIn("method=sendMessage", record)
        self.assertIn("attempt=1", record)
        self.assertIn("duration_ms=", record)
        self.assertIn("outcome=delivered", record)
        self.assertIn("timeout_phase=none", record)
        self.assertIn("family=ipv6", record)
        self.assertNotIn("secret-token", record)
        self.assertNotIn("987654321", record)
        self.assertNotIn("private text", record)

    def test_transport_info_reaches_scoped_worker_stderr_without_secret_fields(self):
        application = logging.getLogger("tak_ili_inache")
        old_handlers, old_level, old_propagate = list(application.handlers), application.level, application.propagate
        for handler in old_handlers:
            application.removeHandler(handler)
        stream = io.StringIO()
        try:
            with patch("sys.stderr", stream):
                configure_logging()
                api, _connector, _factory = self._api([_Response(200, b'{"ok":true,"result":true}')])
                api.send_message(987654321, "private text")
            record = stream.getvalue()
            self.assertIn("INFO tak_ili_inache.telegram_api telegram_transport method=sendMessage", record)
            self.assertIn("outcome=delivered", record)
            self.assertIn("family=ipv6", record)
            self.assertNotIn("secret-token", record)
            self.assertNotIn("987654321", record)
            self.assertNotIn("private text", record)
        finally:
            for handler in list(application.handlers):
                application.removeHandler(handler)
            for handler in old_handlers:
                application.addHandler(handler)
            application.setLevel(old_level)
            application.propagate = old_propagate

    def test_request_timeout_replaces_short_connector_timeout_before_http_write(self):
        class _PreSendConnector:
            def __init__(self, sock): self.sock = sock
            def connect_pre_send(self, _host): return PreSendConnection(self.sock, "ipv6", 1, ())

        long_poll_socket = _Socket()
        api = TelegramApi("123456:secret-token", connector=_PreSendConnector(long_poll_socket), connection_factory=_Factory([_Response(200, b'{"ok":true,"result":[]}')]), delivery_policy=DeliveryPolicy(sleep=lambda _: None))
        self.assertEqual(api.get_updates(None, 30), [])
        self.assertEqual(long_poll_socket.timeouts, [40])

        send_socket = _Socket()
        api = TelegramApi("123456:secret-token", connector=_PreSendConnector(send_socket), connection_factory=_Factory([_Response(200, b'{"ok":true,"result":true}')]), delivery_policy=DeliveryPolicy(sleep=lambda _: None))
        api.send_message(42, "reply")
        self.assertEqual(send_socket.timeouts, [20])

    def test_bot_callback_continues_when_actual_api_ack_and_cleanup_fail(self):
        api, _connector, factory = self._api([
            OSError("ack unavailable"),
            OSError("cleanup unavailable"),
            _Response(200, b'{"ok":true,"result":true}'),
        ])
        repository = FakeRepository()
        repository.save_round(import_fixtures(Path(__file__).resolve().parents[1] / "data" / "fixtures_sample.csv"))
        bot = BotService(repository, api, lambda: repository.get_active_round().deadline_msk)
        bot.handle_update({"callback_query": {"id": "callback-id", "data": "menu:new", "from": {"id": 42}, "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}}}})
        self.assertEqual([item[1].rsplit("/", 1)[-1] for item in factory.requests], ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"])
