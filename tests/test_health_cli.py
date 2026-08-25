from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tak_ili_inache.liveness import LivenessStore
from tak_ili_inache.operations import health


class HealthCliTests(unittest.TestCase):
    def test_require_liveness_prints_safe_json_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            result = subprocess.run([sys.executable, "-m", "tak_ili_inache.health", "--data-dir", directory, "--require-liveness"], text=True, capture_output=True, env=environment, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)["ok"])
            self.assertNotIn("token", result.stdout.lower()); self.assertNotIn("payload", result.stdout.lower())

    def test_delivery_health_recovers_only_after_outbound_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)]
            liveness = LivenessStore(directory, now=lambda: now[0])
            liveness.start(); liveness.successful_poll(); liveness.reply_error()
            self.assertFalse(health(directory, require_liveness=True, require_delivery_health=True, now=lambda: now[0])["delivery_ok"])
            now[0] += timedelta(seconds=1)
            liveness.successful_poll()
            self.assertFalse(health(directory, require_liveness=True, require_delivery_health=True, now=lambda: now[0])["delivery_ok"])
            now[0] += timedelta(seconds=1)
            liveness.reply_delivered()
            report = health(directory, require_liveness=True, require_delivery_health=True, now=lambda: now[0])
            self.assertTrue(report["delivery_ok"])
            self.assertEqual(report["liveness"]["reply_error_count"], 1)

    def test_legacy_reply_error_snapshot_stays_failed_closed_until_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)]
            Path(directory, LivenessStore.filename).write_text(json.dumps({
                "process_started_at": "2026-09-07T08:59:00+00:00",
                "last_successful_poll_at": "2026-09-07T09:00:00+00:00",
                "reply_error_count": 2,
                "last_reply_delivered_at": "2026-09-07T08:58:00+00:00",
            }), encoding="utf-8")
            liveness = LivenessStore(directory, now=lambda: now[0])
            self.assertFalse(liveness.snapshot()["delivery_ok"])
            liveness.successful_poll()
            self.assertFalse(liveness.snapshot()["delivery_ok"])
            now[0] += timedelta(seconds=1)
            liveness.reply_delivered()
            recovered = liveness.snapshot()
            self.assertTrue(recovered["delivery_ok"])
            self.assertEqual(recovered["reply_error_count"], 2)
            self.assertEqual(recovered["last_reply_error_at"], recovered["last_reply_delivered_at"])

    def test_restart_preserves_reply_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)]
            first = LivenessStore(directory, now=lambda: now[0])
            first.start(); first.successful_poll(); first.reply_error()
            now[0] += timedelta(seconds=1)
            restarted = LivenessStore(directory, now=lambda: now[0])
            restarted.start(); restarted.successful_poll()
            snapshot = restarted.snapshot()
            self.assertEqual(snapshot["reply_error_count"], 1)
            self.assertIsNotNone(snapshot["last_reply_error_at"])
            self.assertFalse(snapshot["delivery_ok"])

    def test_v5_contract_derives_current_and_keeps_generic_activate_disabled(self):
        root = Path(__file__).resolve().parents[1]
        wrapper = (root / "deploy" / "tak-ili-inache-admin").read_text(encoding="utf-8")
        self.assertIn('WRAPPER_CAPABILITY="tak-ili-inache-admin:v5-delivery-recovery"', wrapper)
        self.assertIn('activate_transaction() {', wrapper); self.assertIn('[[ $# -eq 2 ]]', wrapper)
        self.assertIn('activate_delivery_recovery() {', wrapper)
        self.assertIn('rollback_target="$(current_rollback_target)"', wrapper)
        self.assertIn('runtime_digest "$rollback_target"', wrapper)
        self.assertIn('CRITICAL: rollback digest changed after candidate failure', wrapper)
        self.assertNotIn("LEGACY_ROLLBACK", wrapper); self.assertNotIn("activate_release", wrapper)
        self.assertEqual((root / "deploy" / "tak-ili-inache-admin.capability").read_text(encoding="utf-8").strip(), "tak-ili-inache-admin:v5-delivery-recovery")
        upgrade = (root / "deploy" / "tak-ili-inache-upgrade-wrapper").read_text(encoding="utf-8")
        self.assertIn('"tak-ili-inache-admin:v5-delivery-recovery"', upgrade)
        self.assertIn('install -o root -g root -m 0750 "$SOURCE/tak-ili-inache-admin" "$STAGE/wrapper"', upgrade)
        self.assertNotIn("EXPECTED_ANCHOR", upgrade); self.assertNotIn('"$STAGE/anchor"', upgrade)

    def test_v3_current_promotes_cjm_candidate_and_candidate_failure_restores_exact_prior(self):
        with self.subTest("success"):
            result, current, prior, candidate, log = self._run_transaction()
            self.assertEqual(result.returncode, 0); self.assertEqual(current, candidate, (result.stdout, result.stderr, log))
            self.assertEqual(log.count("restart tak-ili-inache.service"), 1)
        with self.subTest("health-failure"):
            result, current, prior, candidate, log = self._run_transaction(fail_health_attempts=24)
            self.assertNotEqual(result.returncode, 0); self.assertIn("candidate failed; prior current restored", result.stderr)
            self.assertEqual(current, prior)
            self.assertEqual(log.count("restart tak-ili-inache.service"), 2)
        with self.subTest("restart-failure"):
            result, current, prior, candidate, log = self._run_transaction(fail_restart=True)
            self.assertNotEqual(result.returncode, 0); self.assertEqual(current, prior)
            self.assertEqual(log.count("restart tak-ili-inache.service"), 2)

    def test_tampered_prior_digest_fails_critical_without_masked_restore(self):
        result, current, prior, candidate, log = self._run_transaction(fail_health_attempts=24, tamper_prior_digest=True)
        self.assertNotEqual(result.returncode, 0); self.assertIn("CRITICAL: candidate digest changed", result.stderr)
        self.assertEqual(current, candidate)
        self.assertEqual(log.count("restart tak-ili-inache.service"), 1)

    def test_current_path_escape_and_candidate_symlink_fail_before_switch(self):
        result, current, prior, candidate, log = self._run_transaction(current_escape=True)
        self.assertNotEqual(result.returncode, 0); self.assertIn("current rollback target is invalid", result.stderr); self.assertEqual(log, "")
        result, current, prior, candidate, log = self._run_transaction(candidate_symlink=True)
        self.assertNotEqual(result.returncode, 0); self.assertIn("candidate release is invalid", result.stderr); self.assertEqual(current, prior); self.assertEqual(log, "")

    def test_upgrade_source_mode_0644_installs_target_0750_contract(self):
        root = Path(__file__).resolve().parents[1]
        source = root / "deploy" / "tak-ili-inache-admin"
        self.assertEqual(source.stat().st_mode & 0o777, 0o644)
        upgrade = (root / "deploy" / "tak-ili-inache-upgrade-wrapper").read_text(encoding="utf-8")
        self.assertNotIn('-x "$SOURCE/tak-ili-inache-admin"', upgrade)
        self.assertIn('-f "$SOURCE/tak-ili-inache-admin" && ! -L "$SOURCE/tak-ili-inache-admin"', upgrade)
        self.assertIn('install -o root -g root -m 0750 "$SOURCE/tak-ili-inache-admin" "$STAGE/wrapper"', upgrade)

    def _run_transaction(self, *, fail_health_attempts: int = 0, fail_restart: bool = False, tamper_prior_digest: bool = False, current_escape: bool = False, candidate_symlink: bool = False):
        root = Path(__file__).resolve().parents[1]
        source = (root / "deploy" / "tak-ili-inache-admin").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            sandbox, releases = Path(directory), Path(directory) / "releases"; releases.mkdir()
            prior, candidate, outside = releases / "active-v3", releases / "cjm-v4", Path(directory) / "outside"
            for release in ((prior,) if candidate_symlink else (prior, candidate)):
                (release / ".venv/bin").mkdir(parents=True); (release / "scripts").mkdir(); (release / "deploy").mkdir()
                (release / ".venv/bin/python").write_text("", encoding="utf-8"); (release / ".venv/bin/python").chmod(0o755)
                (release / "scripts/release_digest.py").write_text("", encoding="utf-8")
            if not candidate_symlink:
                (candidate / "deploy/tak-ili-inache-admin.capability").write_text("tak-ili-inache-admin:v5-delivery-recovery\n", encoding="utf-8")
            if current_escape:
                outside.mkdir(); current = sandbox / "current"; current.symlink_to(outside)
            else:
                current = sandbox / "current"; current.symlink_to(prior)
            if candidate_symlink:
                external = sandbox / "candidate-outside"; external.mkdir(); candidate.symlink_to(external)
            wrapper = source.replace("readonly RELEASES_DIR=/opt/tak-ili-inache/releases", f'readonly RELEASES_DIR="{releases}"').replace("readonly CURRENT_LINK=/opt/tak-ili-inache/current", f'readonly CURRENT_LINK="{current}"').replace("readonly ENV_FILE=/etc/tak-ili-inache.env", f'readonly ENV_FILE="{sandbox / "env"}"').replace("readonly ACTIVATION_LOCK=/run/lock/tak-ili-inache-admin-activation.lock", f'readonly ACTIVATION_LOCK="{sandbox / "activation.lock"}"')
            runner = sandbox / "wrapper"; runner.write_text(wrapper, encoding="utf-8"); runner.chmod(0o755)
            fake_bin, log, counter = sandbox / "bin", sandbox / "systemctl.log", sandbox / "counter"; fake_bin.mkdir()
            commands = {
                "readlink": "#!/usr/bin/env bash\npython3 -c 'import os,sys; print(os.path.realpath(sys.argv[-1]))' \"$@\"\n",
                "stat": "#!/usr/bin/env bash\n[[ \"$1\" == \"-c\" ]] || exit 1\n[[ \"$2\" == \"%u\" ]] && echo 0 || echo 755\n",
                "sleep": "#!/usr/bin/env bash\nexit 0\n",
                "flock": "#!/usr/bin/env bash\nexit 0\n",
                "mv": "#!/usr/bin/env bash\nif [[ \"$1\" == \"-Tf\" ]]; then shift; /bin/rm -f \"$2\"; exec /bin/mv -f \"$1\" \"$2\"; fi\nexec /bin/mv \"$@\"\n",
                "systemctl": "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$TII_FAKE_LOG\"\nif [[ \"$1\" == restart ]]; then n=0; [[ -f \"$TII_COUNTER\" ]] && n=$(<\"$TII_COUNTER\"); n=$((n+1)); echo \"$n\" > \"$TII_COUNTER\"; [[ \"${TII_FAIL_RESTART:-0}\" == 1 && \"$n\" == 1 ]] && exit 1; fi\nexit 0\n",
                "runuser": "#!/usr/bin/env bash\njoined=\"$*\"\nif [[ \"$joined\" == *release_digest.py* ]]; then n=0; [[ -f \"$TII_DIGEST_COUNTER\" ]] && n=$(<\"$TII_DIGEST_COUNTER\"); n=$((n+1)); echo \"$n\" > \"$TII_DIGEST_COUNTER\"; [[ \"${TII_TAMPER:-0}\" == 1 && \"$n\" -ge 3 ]] && { echo sha256:tampered; exit 0; }; echo sha256:prior; exit 0; fi\nif [[ \"$joined\" == *tak_ili_inache.health* ]]; then n=0; [[ -f \"$TII_HEALTH_COUNTER\" ]] && n=$(<\"$TII_HEALTH_COUNTER\"); n=$((n+1)); echo \"$n\" > \"$TII_HEALTH_COUNTER\"; [[ \"$n\" -le \"${TII_FAIL_HEALTH:-0}\" ]] && exit 1; fi\nexit 0\n",
            }
            for name, body in commands.items():
                path = fake_bin / name; path.write_text(body, encoding="utf-8"); path.chmod(0o755)
            environment = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}", TII_FAKE_LOG=str(log), TII_COUNTER=str(counter), TII_DIGEST_COUNTER=str(sandbox / "digest-counter"), TII_HEALTH_COUNTER=str(sandbox / "health-counter"), TII_FAIL_HEALTH=str(fail_health_attempts), TII_FAIL_RESTART="1" if fail_restart else "0", TII_TAMPER="1" if tamper_prior_digest else "0")
            result = subprocess.run([str(runner), "activate-transaction", "cjm-v4"], text=True, capture_output=True, env=environment, check=False)
            return result, str(current.resolve()), str(prior.resolve()), str(candidate.resolve()), log.read_text(encoding="utf-8") if log.exists() else ""
