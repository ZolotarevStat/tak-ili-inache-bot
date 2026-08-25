from __future__ import annotations

import configparser
import importlib.util
import importlib.machinery
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"


def _load(name: str, filename: str):
    path = INFRA / filename
    spec = importlib.util.spec_from_file_location(name, path, loader=importlib.machinery.SourceFileLoader(name, str(path)))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OPS = _load("tii_infra_ops", "tii_infra_ops.py")
DIGEST = _load("tii_infra_digest_test", "tii_infra_digest.py")
INSTALLER = _load("tii_infra_installer_test", "tak-ili-inache-infra-upgrade")


class InfraV637SecurityTests(unittest.TestCase):
    def _parse_rows(self, rows: list[str]) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protected.env"
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            # Parser semantics are tested independently of root-owned file
            # metadata; production executes the real metadata check first.
            with patch.object(OPS, "_secret_file"):
                return OPS.parse_protected_env(path, frozenset({"SAFE"}), frozenset({"SAFE"}))

    def test_non_eval_parser_accepts_only_exact_key_value_grammar(self) -> None:
        self.assertEqual(self._parse_rows(["# comment", "", "SAFE=plain-value_123"]), {"SAFE": "plain-value_123"})
        cases = (
            ["OTHER=value"],
            ["SAFE=value", "SAFE=again"],
            ["SAFE =value"],
            ["SAFE=value with-space"],
            ["SAFE="],
            ["SAFE=one", "malformed"],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(OPS.ConfigError):
                    self._parse_rows(rows)

    def test_shell_injection_payloads_are_rejected_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            payloads = (
                f"SAFE=$(touch {marker})",
                f"SAFE=`touch {marker}`",
                "SAFE=value;id",
                "SAFE=value|id",
                "SAFE=value&wait",
                "SAFE=<file",
                "SAFE=\\path",
                "PATH=/tmp/evil",
                "BASH_ENV=/tmp/evil",
                "LD_PRELOAD=/tmp/evil.so",
            )
            for payload in payloads:
                with self.subTest(payload=payload), self.assertRaises(OPS.ConfigError):
                    self._parse_rows([payload])
            self.assertFalse(marker.exists(), "parser must never execute input")

    def test_clean_subprocess_environment_drops_path_bash_env_and_ld(self) -> None:
        with patch.dict(os.environ, {"PATH": "/tmp/evil", "BASH_ENV": "/tmp/evil", "LD_PRELOAD": "/tmp/evil.so", "HOME": "/tmp/evil"}, clear=True):
            environment = OPS.clean_env({"SAFE": "value"})
        self.assertEqual(environment, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "SAFE": "value"})

    def test_backup_operation_has_nonblocking_exclusive_lock(self) -> None:
        source = (INFRA / "tii_infra_ops.py").read_text(encoding="utf-8")
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", source)
        self.assertIn("another backup is already running", source)

    def test_backup_holds_the_application_repository_lock_for_snapshot_scan(self) -> None:
        source = (INFRA / "tii_infra_ops.py").read_text(encoding="utf-8")
        self.assertIn('DATA_DIR / ".repository.lock"', source)
        self.assertIn("os.O_RDONLY | os.O_NOFOLLOW", source)
        self.assertIn("fcntl.flock(descriptor, fcntl.LOCK_EX)", source)
        self.assertLess(source.index("fcntl.flock(descriptor, fcntl.LOCK_EX)"), source.index('[RESTIC, "backup"'))

    def test_restore_validation_rejects_symlink_and_special_file_before_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "var/lib/tak-ili-inache"
            data.mkdir(parents=True)
            (data / "state.csv").write_text("safe", encoding="utf-8")
            self.assertEqual(OPS.validate_restore_tree(root), data.resolve())
            (data / "escape").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(RuntimeError, "unsafe file type"):
                OPS.validate_restore_tree(root)
            (data / "escape").unlink()
            os.mkfifo(data / "named-pipe")
            with self.assertRaisesRegex(RuntimeError, "unsafe file type"):
                OPS.validate_restore_tree(root)

    def test_restore_transfers_private_parent_only_after_tree_validation(self) -> None:
        source = (INFRA / "tii_infra_ops.py").read_text(encoding="utf-8")
        validation = source.index("restored_data = validate_restore_tree(restore_root)")
        parent_chown = source.index("os.chown(restore_root, worker.pw_uid, worker_group.gr_gid)")
        health = source.index('run([RUNUSER, "-u", "takiliinache"')
        self.assertLess(validation, parent_chown)
        self.assertLess(parent_chown, health)

    def test_digest_is_deterministic_for_idempotency_and_rejects_symlink_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "infra"
            shutil.copytree(INFRA, copied)
            before = DIGEST.package_digest(copied)
            self.assertEqual(before, DIGEST.package_digest(copied))
            target = copied / DIGEST.FILES[0]
            target.unlink()
            target.symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                DIGEST.package_digest(copied)

    def _release_tree(self, directory: str) -> tuple[Path, Path, Path]:
        anchor = Path(directory) / "anchor"
        releases = anchor / "opt/tak-ili-inache/releases"
        release = releases / "0.1.0-infra-v6.3.7-test"
        shutil.copytree(INFRA, release / "infra")
        for path in (anchor, anchor / "opt", anchor / "opt/tak-ili-inache", releases, release, release / "infra"):
            path.chmod(0o755)
        return anchor, releases, release

    def test_descriptor_staging_rejects_nested_release_symlink_and_writable_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            anchor, releases, release = self._release_tree(directory)
            stage = Path(directory) / "stage"
            stage.mkdir(mode=0o700)
            kwargs = {"releases_root": releases, "trusted_anchor": anchor, "owner": os.getuid(), "group": os.getgid()}
            with self.assertRaisesRegex(INSTALLER.SourceTrustError, "direct child"):
                INSTALLER.stage_release_sources(release / "nested", stage, **kwargs)
            with self.assertRaisesRegex(INSTALLER.SourceTrustError, "not canonical"):
                INSTALLER.stage_release_sources(release / ".." / release.name, stage, **kwargs)
            (anchor / "opt/tak-ili-inache").chmod(0o775)
            with self.assertRaisesRegex(INSTALLER.SourceTrustError, "ownership or mode"):
                INSTALLER.stage_release_sources(release, stage, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            anchor = Path(directory) / "anchor"
            real_root = anchor / "real-opt/tak-ili-inache/releases"
            release = real_root / "0.1.0-infra-v6.3.7-test"
            shutil.copytree(INFRA, release / "infra")
            (anchor / "real-opt").chmod(0o755)
            (anchor / "opt").symlink_to("real-opt", target_is_directory=True)
            logical_root = anchor / "opt/tak-ili-inache/releases"
            logical_release = logical_root / release.name
            stage = Path(directory) / "stage"
            stage.mkdir(mode=0o700)
            with self.assertRaisesRegex(INSTALLER.SourceTrustError, "missing or symlinked"):
                INSTALLER.stage_release_sources(logical_release, stage, releases_root=logical_root, trusted_anchor=anchor, owner=os.getuid(), group=os.getgid())

    def test_descriptor_staging_detects_replacement_between_validation_and_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            anchor, releases, release = self._release_tree(directory)
            stage = Path(directory) / "stage"
            stage.mkdir(mode=0o700)
            infra = release / "infra"
            first = INSTALLER.SOURCE_FILES[0]

            def replace_after_open(name: str) -> None:
                if name == first:
                    replacement = infra / ".replacement"
                    replacement.write_text("replacement", encoding="utf-8")
                    replacement.chmod(0o644)
                    os.replace(replacement, infra / name)

            with self.assertRaisesRegex(INSTALLER.SourceTrustError, "changed during copy"):
                INSTALLER.stage_release_sources(
                    release,
                    stage,
                    releases_root=releases,
                    trusted_anchor=anchor,
                    owner=os.getuid(),
                    group=os.getgid(),
                    between_validation_and_copy=replace_after_open,
                )

    def test_enable_failure_compensates_exact_backup_and_alloy_units(self) -> None:
        primary = subprocess.CalledProcessError(1, [OPS.SYSTEMCTL, "enable", "--now"])
        for units in (OPS.BACKUP_ENABLE_UNITS, OPS.ALLOY_ENABLE_UNITS):
            with self.subTest(units=units), patch.object(OPS, "run", side_effect=[primary, None, None]) as runner, patch.object(OPS, "assert_disabled_inactive") as asserted:
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    OPS.enable_units_transaction(units)
                self.assertIs(caught.exception, primary)
                self.assertEqual(runner.call_args_list[0].args[0], [OPS.SYSTEMCTL, "enable", "--now", *units])
                self.assertEqual(runner.call_args_list[1].args[0], [OPS.SYSTEMCTL, "disable", "--now", units[0]])
                self.assertEqual(runner.call_args_list[2].args[0], [OPS.SYSTEMCTL, "disable", "--now", units[1]])
                asserted.assert_called_once_with(units)

    def test_backup_and_alloy_paths_delegate_to_transactional_enable(self) -> None:
        with patch.object(OPS, "backup_config"), patch.object(OPS, "_root_marker"), patch.object(OPS, "enable_units_transaction", side_effect=RuntimeError("primary")) as transaction:
            with self.assertRaisesRegex(RuntimeError, "primary"):
                OPS.enable_backup()
            transaction.assert_called_once_with(OPS.BACKUP_ENABLE_UNITS)
        with patch.object(OPS, "grafana_config"), patch.object(OPS.Path, "is_file", return_value=True), patch.object(OPS.os, "access", return_value=True), patch.object(OPS, "enable_units_transaction", side_effect=RuntimeError("primary")) as transaction:
            with self.assertRaisesRegex(RuntimeError, "primary"):
                OPS.enable_alloy()
            transaction.assert_called_once_with(OPS.ALLOY_ENABLE_UNITS)

    def test_alloy_enable_rejects_missing_and_non_executable_binary(self) -> None:
        for is_file, executable in ((False, False), (True, False)):
            with self.subTest(is_file=is_file, executable=executable), \
                patch.object(OPS, "grafana_config"), \
                patch.object(OPS.Path, "is_file", return_value=is_file), \
                patch.object(OPS.os, "access", return_value=executable), \
                patch.object(OPS, "enable_units_transaction") as transaction:
                with self.assertRaisesRegex(RuntimeError, "package is not installed"):
                    OPS.enable_alloy()
                transaction.assert_not_called()

    def test_protected_file_creation_rejects_all_broken_symlink_entries(self) -> None:
        self.assertEqual(
            INSTALLER.PROTECTED_FILES,
            (
                Path("/etc/tak-ili-inache-backup.env"),
                Path("/etc/tak-ili-inache-restic-password"),
                Path("/etc/tak-ili-inache-grafana.env"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for protected in INSTALLER.PROTECTED_FILES:
                with self.subTest(name=protected.name):
                    path = Path(directory) / protected.name
                    path.symlink_to("missing-target")
                    with self.assertRaisesRegex(RuntimeError, "symlink"):
                        INSTALLER.create_protected_file(path, [], owner=os.getuid(), group=os.getgid())
                    self.assertTrue(path.is_symlink())

    def test_protected_file_creation_is_idempotent_and_rollback_removes_only_exact_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / protected.name for protected in INSTALLER.PROTECTED_FILES]
            created: list[INSTALLER.CreatedProtectedFile] = []
            for path in paths:
                INSTALLER.create_protected_file(path, created, owner=os.getuid(), group=os.getgid())
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(created), 3)
            again: list[INSTALLER.CreatedProtectedFile] = []
            for path in paths:
                INSTALLER.create_protected_file(path, again, owner=os.getuid(), group=os.getgid())
            self.assertEqual(again, [])
            replaced = paths[0]
            replaced.unlink()
            replaced.symlink_to("never-follow-this")
            INSTALLER._remove_created_protected(created)
            self.assertTrue(replaced.is_symlink())
            self.assertFalse(paths[1].exists())
            self.assertFalse(paths[2].exists())


class InfraV637InstallerContractTests(unittest.TestCase):
    def test_capability_and_fixed_least_privilege_allowlist(self) -> None:
        self.assertEqual(
            (INFRA / "tak-ili-inache-infra-admin.capability").read_text(encoding="utf-8").strip(),
            "tak-ili-inache-infra-admin:v6.3.7-operational-status",
        )
        sudoers = (INFRA / "tak-ili-inache-infra-admin.sudoers").read_text(encoding="utf-8")
        self.assertIn("TII_APP_ADMIN = /usr/local/sbin/tak-ili-inache-admin *", sudoers)
        self.assertNotIn("tak-ili-inache-infra-admin *", sudoers)
        for command in ("capability", "status", "backup-init", "backup-run", "backup-restore-verify", "backup-enable", "alloy-enable"):
            self.assertIn(f"tak-ili-inache-infra-admin {command}", sudoers)
        for path in ("/etc/tak-ili-inache.env", "/etc/tak-ili-inache-backup.env", "/etc/tak-ili-inache-restic-password", "/etc/tak-ili-inache-grafana.env"):
            self.assertIn(f"sudoedit {path}", sudoers)

    def test_shell_dispatchers_never_source_env_or_use_relative_tools(self) -> None:
        scripts = [
            INFRA / "tak-ili-inache-infra-admin",
            INFRA / "tak-ili-inache-backup-run",
            INFRA / "tak-ili-inache-backup-freshness-run",
            INFRA / "tak-ili-inache-alloy-run",
        ]
        subprocess.run(["/bin/bash", "-n", *map(str, scripts)], check=True)
        for script in scripts:
            text = script.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/bash"), script)
            self.assertNotIn("EnvironmentFile", text)
            self.assertNotRegex(text, r"(^|\n)\s*(source|\.)\s+.*env")
        admin = scripts[0].read_text(encoding="utf-8")
        self.assertIn("/usr/bin/env -i", admin)
        self.assertIn("/usr/bin/python3", admin)

    def test_installer_stages_explicit_allowlist_before_validation_and_install(self) -> None:
        installer = (INFRA / "tak-ili-inache-infra-upgrade").read_text(encoding="utf-8")
        self.assertIn("SOURCE_FILES", installer)
        self.assertIn("stage_release_sources", installer)
        self.assertIn("TemporaryDirectory(prefix=\"tak-ili-inache-infra-v637.", installer)
        self.assertIn("tii_infra_digest.py", installer)
        self.assertIn("infra_source_digest=", installer)
        self.assertIn("O_NOFOLLOW", installer)
        self.assertIn("dir_fd=", installer)
        self.assertIn("source entry changed during copy", installer)
        self.assertIn("release must be a direct child", installer)
        self.assertIn("relative_root.parts", installer)

    def test_digest_manifest_is_unique_and_exactly_matches_install_source_manifest(self) -> None:
        self.assertEqual(len(DIGEST.FILES), len(set(DIGEST.FILES)))
        self.assertEqual(tuple(DIGEST.FILES), tuple(INSTALLER.SOURCE_FILES))

    def test_protected_file_contract_uses_lstat_no_follow_exclusive_create_and_identity_rollback(self) -> None:
        installer = (INFRA / "tak-ili-inache-infra-upgrade").read_text(encoding="utf-8")
        self.assertIn("os.lstat(path)", installer)
        self.assertIn("os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW", installer)
        self.assertIn("_protected_file_info(created_info", installer)
        self.assertIn("CreatedProtectedFile(path=path, identity=_identity", installer)
        self.assertIn("_remove_created_protected", installer)

    def test_strict_systemd_helper_allows_static_inactive_and_rejects_unsafe_or_ambiguous_states(self) -> None:
        states = {
            "tak-ili-inache-backup.service": "static",
            "tak-ili-inache-backup.timer": "disabled",
            "tak-ili-inache-backup-freshness.service": "indirect",
            "tak-ili-inache-backup-freshness.timer": "not-found",
            "tak-ili-inache-metrics-snapshot.service": "static",
            "tak-ili-inache-metrics-snapshot.timer": "disabled",
            "tak-ili-inache-alloy.service": "indirect",
        }
        active_states = {unit: ("inactive", 3, "") for unit in states}
        enabled_errors = {unit: "" for unit in states}
        returned_codes = dict(OPS.SYSTEMD_ENABLED_RETURN_CODES)

        def fake_systemctl(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            verb, unit = command[1], command[-1]
            if verb == "is-enabled":
                state = states[unit]
                returncode = returned_codes.get(state, 1)
                return subprocess.CompletedProcess(command, returncode, f"{state}\n" if state else "", enabled_errors[unit])
            self.assertEqual(verb, "is-active")
            state, returncode, stderr = active_states[unit]
            return subprocess.CompletedProcess(command, returncode, f"{state}\n" if state else "", stderr)

        with patch.object(OPS.subprocess, "run", side_effect=fake_systemctl):
            INSTALLER.assert_units_disabled_inactive()

            states["tak-ili-inache-backup.timer"] = "enabled"
            with self.assertRaisesRegex(RuntimeError, "tak-ili-inache-backup.timer: enabled"):
                INSTALLER.assert_units_disabled_inactive()

            states["tak-ili-inache-backup.timer"] = "disabled"
            active_states["tak-ili-inache-backup.service"] = ("active", 0, "")
            with self.assertRaisesRegex(OPS.SystemdStateError, "did not confirm inactive"):
                INSTALLER.assert_units_disabled_inactive()

            active_states["tak-ili-inache-backup.service"] = ("inactive", 3, "")
            enabled_errors["tak-ili-inache-backup.service"] = "Failed to connect to bus"
            with self.assertRaisesRegex(OPS.SystemdStateError, "systemd is-enabled failed"):
                INSTALLER.assert_units_disabled_inactive()

            enabled_errors["tak-ili-inache-backup.service"] = ""
            states["tak-ili-inache-backup.service"] = ""
            with self.assertRaisesRegex(OPS.SystemdStateError, "empty state"):
                INSTALLER.assert_units_disabled_inactive()

            states["tak-ili-inache-backup.service"] = "mystery"
            with self.assertRaisesRegex(OPS.SystemdStateError, "unknown state"):
                INSTALLER.assert_units_disabled_inactive()

            states["tak-ili-inache-backup.service"] = "static"
            returned_codes["static"] = 1
            with self.assertRaisesRegex(OPS.SystemdStateError, "inconsistent state"):
                INSTALLER.assert_units_disabled_inactive()

    def test_systemd_state_helper_rejects_dbus_error(self) -> None:
        failure = subprocess.CompletedProcess([OPS.SYSTEMCTL, "is-enabled", "unit.service"], 1, "", "Failed to connect to bus")
        with patch.object(OPS.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(OPS.SystemdStateError, "systemd is-enabled failed"):
                OPS.assert_disabled_inactive(("unit.service",))

    def test_operational_status_accepts_only_stable_active_or_inactive(self) -> None:
        for state, returncode in (("active", 0), ("inactive", 3)):
            completed = subprocess.CompletedProcess([OPS.SYSTEMCTL, "is-active", "unit.timer"], returncode, f"{state}\n", "")
            with self.subTest(state=state), patch.object(OPS.subprocess, "run", return_value=completed):
                self.assertEqual(OPS.systemd_runtime_state("unit.timer"), state)
        for state, returncode in (("failed", 3), ("active", 3), ("activating", 0)):
            completed = subprocess.CompletedProcess([OPS.SYSTEMCTL, "is-active", "unit.timer"], returncode, f"{state}\n", "")
            with self.subTest(state=state), patch.object(OPS.subprocess, "run", return_value=completed):
                with self.assertRaises(OPS.SystemdStateError):
                    OPS.systemd_runtime_state("unit.timer")

    def test_systemd_state_helper_rejects_empty_stdout(self) -> None:
        empty = subprocess.CompletedProcess([OPS.SYSTEMCTL, "is-enabled", "unit.service"], 1, "", "")
        with patch.object(OPS.subprocess, "run", return_value=empty):
            with self.assertRaisesRegex(OPS.SystemdStateError, "empty state"):
                OPS.assert_disabled_inactive(("unit.service",))

    def test_systemd_state_helper_rejects_unknown_state(self) -> None:
        unknown = subprocess.CompletedProcess([OPS.SYSTEMCTL, "is-enabled", "unit.service"], 1, "mystery\n", "")
        with patch.object(OPS.subprocess, "run", return_value=unknown):
            with self.assertRaisesRegex(OPS.SystemdStateError, "unknown state"):
                OPS.assert_disabled_inactive(("unit.service",))

    def test_installer_fails_before_mutation_for_active_units_and_rechecks_after(self) -> None:
        installer = (INFRA / "tak-ili-inache-infra-upgrade").read_text(encoding="utf-8")
        self.assertGreaterEqual(installer.count("assert_units_disabled_inactive"), 3)
        self.assertIn("from tii_infra_ops import assert_disabled_inactive", installer)
        self.assertNotIn('"is-enabled", "--quiet"', installer)
        self.assertNotIn('or "not-found"', installer)
        self.assertNotIn("systemctl disable", installer)
        self.assertNotIn("systemctl stop", installer)
        self.assertIn("infra_units=installed-disabled", installer)
        self.assertIn("_restore_existing", installer)
        self.assertIn("TemporaryDirectory", installer)
        self.assertIn("infra upgrade already running", installer)
        self.assertIn("/var/backups/tak-ili-inache-infra-upgrade", installer)

    def test_units_have_no_environment_file_and_alloy_is_read_only_for_metrics(self) -> None:
        units = [
            INFRA / "tak-ili-inache-backup.service",
            INFRA / "tak-ili-inache-backup.timer",
            INFRA / "tak-ili-inache-backup-freshness.service",
            INFRA / "tak-ili-inache-backup-freshness.timer",
            INFRA / "tak-ili-inache-metrics-snapshot.service",
            INFRA / "tak-ili-inache-metrics-snapshot.timer",
            INFRA / "tak-ili-inache-alloy.service",
        ]
        for unit in units:
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read(unit, encoding="utf-8")
            self.assertTrue(parser.sections(), unit)
            self.assertNotIn("EnvironmentFile", unit.read_text(encoding="utf-8"))
        alloy = (INFRA / "tak-ili-inache-alloy.service").read_text(encoding="utf-8")
        self.assertIn("MemoryHigh=64M", alloy)
        self.assertIn("MemoryMax=96M", alloy)
        self.assertIn("ReadOnlyPaths=/var/lib/tak-ili-inache-metrics", alloy)
        self.assertIn("ReadWritePaths=/var/lib/tak-ili-inache-alloy", alloy)
        self.assertIn("ConditionPathExists=/usr/bin/alloy", alloy)
        self.assertNotIn("ConditionPathIsExecutable", alloy)
        self.assertNotIn("/var/lib/tak-ili-inache ", alloy)

    def test_metrics_contract_is_secret_free_and_textfile_only(self) -> None:
        config = (INFRA / "tak-ili-inache-alloy.config.alloy").read_text(encoding="utf-8")
        self.assertIn("prometheus.remote_write", config)
        self.assertIn("textfile", config)
        self.assertIn("/var/lib/tak-ili-inache-metrics", config)
        self.assertNotIn("loki.", config)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", config)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", config)
        snapshot = (INFRA / "tak-ili-inache-metrics-snapshot").read_text(encoding="utf-8")
        self.assertIn("tak_ili_inache_delivery_ok", snapshot)
        self.assertIn("tak_ili_inache_backup_last_freshness_timestamp_seconds", snapshot)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", snapshot)


if __name__ == "__main__":
    unittest.main()
