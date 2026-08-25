from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from tak_ili_inache import bootstrap_ids


class BootstrapIdsTests(unittest.TestCase):
    def test_helper_prints_only_ids_from_message_and_never_the_token(self) -> None:
        secret = "123456:secret-token-must-not-appear"
        output, errors = io.StringIO(), io.StringIO()
        updates = [
            {"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42}}},
            {"update_id": 2, "message": {"from": {"id": 99}, "chat": {"id": -100123}}},
            {"update_id": 3, "edited_message": {"from": {"id": 777}, "chat": {"id": 777}}},
        ]
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": secret}, clear=True), \
             patch("tak_ili_inache.bootstrap_ids.TelegramApi") as api, \
             redirect_stdout(output), redirect_stderr(errors):
            api.return_value.get_updates.return_value = updates
            bootstrap_ids.main()
        api.assert_called_once_with(secret)
        api.return_value.get_updates.assert_called_once_with(offset=None, timeout=0)
        self.assertEqual(output.getvalue().strip(), '[{"message_from_id": 42, "message_chat_id": 42}, {"message_from_id": 99, "message_chat_id": -100123}]')
        self.assertNotIn(secret, output.getvalue() + errors.getvalue())

    def test_helper_masks_transport_errors(self) -> None:
        secret = "123456:secret-token-must-not-appear"
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": secret}, clear=True), \
             patch("tak_ili_inache.bootstrap_ids.TelegramApi") as api, \
             redirect_stdout(output), redirect_stderr(errors):
            api.return_value.get_updates.side_effect = RuntimeError(f"failed with {secret}")
            with self.assertRaises(SystemExit) as raised:
                bootstrap_ids.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Could not read Telegram updates", errors.getvalue())
        self.assertNotIn(secret, output.getvalue() + errors.getvalue())
