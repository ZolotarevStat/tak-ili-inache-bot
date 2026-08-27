from __future__ import annotations

import csv
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tak_ili_inache.bot import BotService
from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.fake_repository import FakeRepository
from tak_ili_inache.operations import create_backup, health
from test_bot_flow import FakeTelegram
from test_validators import valid_prediction


MOSCOW = ZoneInfo("Europe/Moscow")


def _labels(markup: dict | None) -> list[str]:
    return [button["text"] for row in (markup or {}).get("inline_keyboard", []) for button in row]


def _callback_for(markup: dict | None, label: str) -> str:
    for row in (markup or {}).get("inline_keyboard", []):
        for button in row:
            if button["text"] == label:
                return button["callback_data"]
    raise AssertionError(f"button not found: {label}")


class AdminGrantFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FakeRepository()
        self.telegram = FakeTelegram()
        self.bot = BotService(
            self.repo,
            self.telegram,
            lambda: datetime(2026, 8, 28, 12, tzinfo=MOSCOW),
            admin_ids={"99"},
        )
        self.repo.register_participant("99", "Антон")
        self.vitaly = self.repo.register_participant("777", "Vitaly")
        self.repo.register_participant("778", "Мария")
        original = valid_prediction()
        self.prediction = original.__class__("R1", self.vitaly.participant_id, original.bets, original.submitted_at_msk)
        self.repo.save_prediction(self.prediction, "existing-vitaly-prediction")

    def _message(self, actor: str, text: str, chat_type: str = "private") -> None:
        self.bot.handle_update({
            "message": {
                "chat": {"id": 9 if chat_type == "private" else -9, "type": chat_type},
                "from": {"id": int(actor), "first_name": "Тестер"},
                "text": text,
            }
        })

    def _callback(self, actor: str, data: str, chat_type: str = "private") -> None:
        self.bot.handle_update({
            "callback_query": {
                "id": f"cb-{len(self.telegram.answered)}",
                "from": {"id": int(actor), "first_name": "Тестер"},
                "data": data,
                "message": {"message_id": 42, "chat": {"id": 9 if chat_type == "private" else -9, "type": chat_type}},
            }
        })

    def _grant_vitaly(self) -> str:
        self._message("99", "/admin")
        self._callback("99", "admin:admins")
        self._callback("99", "admin:admins:add:0")
        markup = self.telegram.messages[-1][2]
        select = _callback_for(markup, "Vitaly")
        self.assertNotIn("777", select)
        self._callback("99", select)
        confirm = _callback_for(self.telegram.messages[-1][2], "Подтвердить")
        self.assertLessEqual(len(confirm.encode("utf-8")), 64)
        self._callback("99", confirm)
        return confirm

    def test_registered_vitaly_gets_immediate_admin_rights_without_raw_id_in_ui(self) -> None:
        confirm = self._grant_vitaly()
        self.assertTrue(self.bot._is_admin("777"))
        self.assertEqual([grant.telegram_id for grant in self.repo.active_admin_grants()], ["777"])
        self.assertNotEqual(self.repo.active_admin_grants()[0].granted_by, "99")
        self.assertEqual(len(self.repo.participants()), 3)
        self.assertEqual(self.repo.get_prediction("R1", self.vitaly.participant_id), self.prediction)
        self._message("777", "/admin")
        self.assertIn("🛠️ Админские команды", self.telegram.messages[-1][1])
        visible = "\n".join(text + str(markup) for _, text, markup in self.telegram.messages)
        self.assertNotIn("777", visible)
        self.assertNotIn("99", visible)
        # Same callback is a one-shot token: a Telegram retry cannot duplicate a grant.
        self._callback("99", confirm)
        self.assertEqual(len(self.repo.active_admin_grants()), 1)
        self.assertIn("устарело", self.telegram.messages[-1][1])

    def test_revoke_is_confirmed_and_bootstrap_admin_is_not_a_ui_target(self) -> None:
        self._grant_vitaly()
        self._callback("99", "admin:admins")
        self._callback("99", "admin:admins:revoke")
        labels = _labels(self.telegram.messages[-1][2])
        self.assertIn("Vitaly", labels)
        self.assertNotIn("Антон", labels)
        select = _callback_for(self.telegram.messages[-1][2], "Vitaly")
        self._callback("99", select)
        confirm = _callback_for(self.telegram.messages[-1][2], "Подтвердить отзыв")
        self._callback("99", confirm)
        self.assertFalse(self.bot._is_admin("777"))
        self.assertTrue(self.bot._is_admin("99"))
        self._callback("99", confirm)
        self.assertFalse(self.repo.active_admin_grants())
        self.assertIn("устарело", self.telegram.messages[-1][1])
        self._message("777", "/admin")
        self.assertIn("только администратору", self.telegram.messages[-1][1])

    def test_non_admin_group_forged_and_stale_callbacks_cannot_mutate_grants(self) -> None:
        self._callback("777", "admin:admins")
        self.assertFalse(self.repo.active_admin_grants())
        self._callback("99", "admin:admins:confirm:forged")
        self.assertFalse(self.repo.active_admin_grants())
        self._callback("99", "admin:admins", chat_type="group")
        self.assertFalse(self.repo.active_admin_grants())
        self.assertIn("личном", self.telegram.messages[-1][1])
        # Opening another screen invalidates all older selection tokens.
        self._message("99", "/admin")
        self._callback("99", "admin:admins:add:0")
        stale = _callback_for(self.telegram.messages[-1][2], "Vitaly")
        # A token belongs to its issuing administrator, even when another
        # bootstrap administrator is also trusted.
        self.bot.admin_ids.add("98")
        self._callback("98", stale)
        self.assertFalse(self.repo.active_admin_grants())
        self._callback("99", "admin:admins")
        self._callback("99", stale)
        self.assertFalse(self.repo.active_admin_grants())
        self.assertIn("устарело", self.telegram.messages[-1][1])

    def test_second_candidate_selection_is_invalidated_before_confirmation(self) -> None:
        self._message("99", "/admin")
        self._callback("99", "admin:admins:add:0")
        markup = self.telegram.messages[-1][2]
        vitaly = _callback_for(markup, "Vitaly")
        maria = _callback_for(markup, "Мария")
        self._callback("99", vitaly)
        self._callback("99", maria)
        self.assertFalse(self.repo.active_admin_grants())
        self.assertIn("устарело", self.telegram.messages[-1][1])

    def test_old_confirmations_cannot_cross_aba_permission_epochs(self) -> None:
        # An old grant confirmation sees the same inactive shape after a
        # grant→revoke cycle, but its expected immutable revision is stale.
        inactive = self.repo.admin_grant_revision("777")
        old_grant = self.bot._issue_admin_action("99", "grant-confirm", "777", inactive)
        self.assertTrue(self.repo.grant_admin("777", self.vitaly.participant_id, "other", "g1", inactive))
        first_active = self.repo.admin_grant_revision("777")
        self.assertTrue(self.repo.revoke_admin("777", "other", "r1", first_active))
        self._callback("99", f"admin:admins:confirm:{old_grant}")
        self.assertFalse(self.repo.active_admin_grants())
        self.assertIn("устарело", self.telegram.messages[-1][1])

        # The mirror image: a revoke confirmation from an earlier active
        # epoch cannot remove a later re-grant.
        after_revoke = self.repo.admin_grant_revision("777")
        self.assertTrue(self.repo.grant_admin("777", self.vitaly.participant_id, "other", "g2", after_revoke))
        old_revoke_revision = self.repo.admin_grant_revision("777")
        old_revoke = self.bot._issue_admin_action("99", "revoke-confirm", "777", old_revoke_revision)
        self.assertTrue(self.repo.revoke_admin("777", "other", "r2", old_revoke_revision))
        regrant_revision = self.repo.admin_grant_revision("777")
        self.assertTrue(self.repo.grant_admin("777", self.vitaly.participant_id, "other", "g3", regrant_revision))
        self._callback("99", f"admin:admins:confirm:{old_revoke}")
        self.assertTrue(self.repo.active_admin_grants())
        self.assertEqual(self.repo.active_admin_grants()[0].telegram_id, "777")
        self.assertIn("устарело", self.telegram.messages[-1][1])


class CsvAdminGrantTests(unittest.TestCase):
    def test_pre_reference_log_migrates_without_changing_its_permission_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory)
            repo.register_participant("777", "Vitaly")
            fields = ["event_id", "telegram_id", "participant_id", "action", "granted_by", "granted_at", "revoked_by", "revoked_at", "active"]
            rows = [
                {"event_id": "grant-1", "telegram_id": "777", "participant_id": "tg:777", "action": "grant", "granted_by": "actor", "granted_at": "g1", "revoked_by": "", "revoked_at": "", "active": "true"},
                {"event_id": "revoke-1", "telegram_id": "777", "participant_id": "tg:777", "action": "revoke", "granted_by": "actor", "granted_at": "g1", "revoked_by": "actor", "revoked_at": "r1", "active": "false"},
            ]
            with (Path(directory) / "admin_grants.csv").open("w", encoding="utf-8", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            restarted = CsvRepository(directory)
            self.assertEqual(restarted.active_admin_grants(), ())
            with (Path(directory) / "admin_grants.csv").open(encoding="utf-8", newline="") as source:
                migrated = list(csv.DictReader(source))
            self.assertEqual(migrated[0]["grant_event_id"], "grant-1")
            self.assertEqual(migrated[1]["grant_event_id"], "grant-1")

    def test_grant_history_is_atomic_idempotent_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory)
            vitaly = repo.register_participant("777", "Vitaly")
            initial = repo.admin_grant_revision("777")
            self.assertEqual(initial, "none")
            self.assertTrue(repo.grant_admin("777", vitaly.participant_id, "actor:masked", "2026-08-28T12:00:00+03:00", initial))
            granted = repo.admin_grant_revision("777")
            self.assertNotEqual(granted, initial)
            self.assertFalse(repo.grant_admin("777", vitaly.participant_id, "actor:masked", "2026-08-28T12:01:00+03:00", initial))
            restarted = CsvRepository(directory)
            self.assertEqual(restarted.active_admin_grants()[0].participant_id, vitaly.participant_id)
            self.assertEqual(restarted.active_admin_grants()[0].revision, granted)
            restarted_bot = BotService(restarted, FakeTelegram(), lambda: datetime(2026, 8, 28, 12, tzinfo=MOSCOW))
            self.assertTrue(restarted_bot._is_admin("777"))
            self.assertTrue(restarted.revoke_admin("777", "actor:masked", "2026-08-28T12:02:00+03:00", granted))
            self.assertFalse(restarted.revoke_admin("777", "actor:masked", "2026-08-28T12:03:00+03:00", granted))
            recovered = CsvRepository(directory)
            self.assertEqual(recovered.active_admin_grants(), ())
            path = Path(directory) / "admin_grants.csv"
            with path.open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(rows[0]), {"event_id", "grant_event_id", "telegram_id", "participant_id", "action", "granted_by", "granted_at", "revoked_by", "revoked_at", "active"})
            self.assertEqual([row["action"] for row in rows], ["grant", "revoke"])
            self.assertEqual(rows[-1]["active"], "false")
            self.assertEqual(rows[-1]["grant_event_id"], rows[0]["event_id"])
            archive = Path(directory).parent / "admin-grants-backup.tar.gz"
            create_backup(directory, archive)
            with tarfile.open(archive) as bundle:
                self.assertIn("admin_grants.csv", bundle.getnames())

    def test_aba_revisions_reject_old_grant_and_revoke_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = CsvRepository(directory)
            vitaly = repo.register_participant("777", "Vitaly")
            initial = repo.admin_grant_revision("777")
            self.assertTrue(repo.grant_admin("777", vitaly.participant_id, "actor", "g1", initial))
            first_grant = repo.admin_grant_revision("777")
            self.assertTrue(repo.revoke_admin("777", "actor", "r1", first_grant))
            after_first_revoke = repo.admin_grant_revision("777")
            # Old grant confirmation now sees the same inactive state shape,
            # but its immutable expected revision cannot be reused (ABA).
            self.assertTrue(repo.grant_admin("777", vitaly.participant_id, "actor", "g2", after_first_revoke))
            second_grant = repo.admin_grant_revision("777")
            self.assertTrue(repo.revoke_admin("777", "actor", "r2", second_grant))
            self.assertFalse(repo.grant_admin("777", vitaly.participant_id, "actor", "stale", after_first_revoke))
            restarted = CsvRepository(directory)
            inactive_revision = restarted.admin_grant_revision("777")
            self.assertTrue(restarted.grant_admin("777", vitaly.participant_id, "actor", "g3", inactive_revision))
            # A stale revoke token from the first active epoch cannot revoke
            # the newly granted right after restart.
            self.assertFalse(restarted.revoke_admin("777", "actor", "stale", first_grant))
            self.assertTrue(restarted.active_admin_grants())

    def test_corrupt_grant_logs_fail_closed_in_repository_health_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = CsvRepository(directory)
            base.register_participant("777", "Vitaly")
            fields = ["event_id", "grant_event_id", "telegram_id", "participant_id", "action", "granted_by", "granted_at", "revoked_by", "revoked_at", "active"]
            valid = {"event_id": "event-1", "grant_event_id": "event-1", "telegram_id": "777", "participant_id": "tg:777", "action": "grant", "granted_by": "actor", "granted_at": "g1", "revoked_by": "", "revoked_at": "", "active": "true"}
            cases = {
                "action": [{**valid, "action": "CORRUPT"}],
                "missing_audit": [{**valid, "granted_by": ""}],
                "invalid_sequence": [{**valid, "action": "revoke", "active": "false", "revoked_by": "actor", "revoked_at": "r1"}],
                "wrong_grant_reference": [
                    valid,
                    {
                        **valid,
                        "event_id": "event-2",
                        "grant_event_id": "other-grant",
                        "action": "revoke",
                        "active": "false",
                        "revoked_by": "actor",
                        "revoked_at": "r1",
                    },
                ],
                "participant_mismatch": [{**valid, "participant_id": "tg:other"}],
            }
            for name, rows in cases.items():
                with self.subTest(name=name):
                    path = Path(directory) / "admin_grants.csv"
                    with path.open("w", encoding="utf-8", newline="") as target:
                        writer = csv.DictWriter(target, fieldnames=fields)
                        writer.writeheader(); writer.writerows(rows)
                    with self.assertRaises(ValueError):
                        CsvRepository(directory)
                    self.assertFalse(health(directory)["ok"])
                    with self.assertRaises(ValueError):
                        create_backup(directory, Path(directory).parent / f"bad-{name}.tar.gz")
