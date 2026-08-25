from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class WrapperV5Tests(unittest.TestCase):
    def test_normal_green_prior_keeps_generic_v4_transaction_semantics(self) -> None:
        result, current, prior, candidate, log = self._run()
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr, log))
        self.assertEqual(current, candidate)
        self.assertEqual(log.count("restart tak-ili-inache.service"), 1)

    def test_readiness_marker_follows_liveness_and_only_reply_delivery_succeeds(self) -> None:
        result, current, prior, candidate, log = self._run(recovery=True, delivery_ready_after=2)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr, log))
        self.assertEqual(current, candidate)
        self.assertIn("candidate_delivery_ready\n", result.stdout)
        self.assertLess(result.stdout.index("candidate_delivery_ready"), result.stdout.index("delivery recovery transaction passed"))
        self.assertIn("delivery 1", log, "a poll/restart alone must remain delivery-red")
        self.assertIn("delivery 2", log, "only simulated reply_delivered may pass strict delivery health")
        self.assertEqual(log.count("restart tak-ili-inache.service"), 1)

    def test_timeout_or_candidate_reply_error_restores_exact_legacy_red_prior(self) -> None:
        for label in ("timeout", "reply-error"):
            with self.subTest(label=label):
                result, current, prior, candidate, log = self._run(
                    recovery=True,
                    delivery_ready_after=0,
                    candidate_reply_error=label == "reply-error",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(current, prior, (result.stdout, result.stderr, log))
                self.assertIn("candidate_delivery_ready", result.stdout)
                self.assertIn("candidate failed; prior current restored; delivery baseline remains red", result.stderr)
                self.assertNotIn("unexpectedly green", result.stderr.lower())
                self.assertGreaterEqual(log.count("delivery "), 24)
                self.assertEqual(log.count("restart tak-ili-inache.service"), 2)

    def test_nonlegacy_baseline_is_fail_closed_before_any_switch(self) -> None:
        result, current, prior, candidate, log = self._run(recovery=True, baseline="green")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(current, prior)
        self.assertIn("delivery recovery baseline is not exact legacy-red", result.stderr)
        self.assertNotIn("restart tak-ili-inache.service", log)

    def test_tampered_prior_candidate_digest_or_path_is_critical(self) -> None:
        cases = (
            ("prior-digest", {"tamper": "prior"}, "CRITICAL: rollback digest changed"),
            ("candidate-digest", {"tamper": "candidate"}, "CRITICAL: candidate digest changed"),
            ("prior-path", {"current_escape": True}, "CRITICAL: current rollback target validation failed"),
        )
        for label, options, expected in cases:
            with self.subTest(label=label):
                result, current, prior, candidate, log = self._run(recovery=True, delivery_ready_after=0, **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                if label == "prior-path":
                    self.assertEqual(log, "")
                else:
                    self.assertEqual(current, candidate)
                    self.assertEqual(log.count("restart tak-ili-inache.service"), 1)

    def test_second_invocation_is_idempotent_and_lock_rejects_parallel_transaction(self) -> None:
        first, current, prior, candidate, log, second = self._run(recovery=True, delivery_ready_after=1, invoke_twice=True)
        self.assertEqual(first.returncode, 0)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(current, candidate)
        self.assertIn("candidate is already current", second.stderr)
        self.assertEqual(log.count("restart tak-ili-inache.service"), 1)

        locked, current, prior, candidate, log = self._run(recovery=True, fail_lock=True)
        self.assertNotEqual(locked.returncode, 0)
        self.assertEqual(current, prior)
        self.assertIn("activation already in progress", locked.stderr)
        self.assertEqual(log, "")

    def test_runtime_digest_includes_all_wrapper_hardening_files(self) -> None:
        from scripts.release_digest import included_files

        root = Path(__file__).resolve().parents[1]
        files = {path.relative_to(root).as_posix() for path in included_files(root)}
        self.assertTrue({
            "deploy/tak-ili-inache-admin",
            "deploy/tak-ili-inache-admin.capability",
            "deploy/tak-ili-inache-admin.sudoers",
            "deploy/tak-ili-inache-upgrade-wrapper",
        }.issubset(files))

    def _run(
        self,
        *,
        recovery: bool = False,
        baseline: str = "legacy-red",
        delivery_ready_after: int = 1,
        candidate_reply_error: bool = False,
        tamper: str = "",
        current_escape: bool = False,
        fail_lock: bool = False,
        invoke_twice: bool = False,
    ):
        root = Path(__file__).resolve().parents[1]
        source = (root / "deploy" / "tak-ili-inache-admin").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            releases = sandbox / "releases"
            releases.mkdir()
            prior, candidate = releases / "prior-green-or-legacy-red", releases / "delivery-health-candidate"
            for release in (prior, candidate):
                (release / ".venv/bin").mkdir(parents=True)
                (release / "scripts").mkdir()
                (release / "deploy").mkdir()
                python = release / ".venv/bin/python"
                python.write_text("", encoding="utf-8")
                python.chmod(0o755)
                (release / "scripts/release_digest.py").write_text("", encoding="utf-8")
            (candidate / "deploy/tak-ili-inache-admin.capability").write_text(
                "tak-ili-inache-admin:v5-delivery-recovery\n", encoding="utf-8"
            )
            current = sandbox / "current"
            if current_escape:
                outside = sandbox / "outside"
                outside.mkdir()
                current.symlink_to(outside)
            else:
                current.symlink_to(prior)

            wrapper = (
                source.replace("readonly RELEASES_DIR=/opt/tak-ili-inache/releases", f'readonly RELEASES_DIR="{releases}"')
                .replace("readonly CURRENT_LINK=/opt/tak-ili-inache/current", f'readonly CURRENT_LINK="{current}"')
                .replace("readonly ACTIVATION_LOCK=/run/lock/tak-ili-inache-admin-activation.lock", f'readonly ACTIVATION_LOCK="{sandbox / "activation.lock"}"')
            )
            runner = sandbox / "wrapper"
            runner.write_text(wrapper, encoding="utf-8")
            runner.chmod(0o755)
            fake_bin, log = sandbox / "bin", sandbox / "operations.log"
            fake_bin.mkdir()
            commands = {
                "readlink": "#!/usr/bin/env bash\npython3 -c 'import os,sys; print(os.path.realpath(sys.argv[-1]))' \"$@\"\n",
                "stat": "#!/usr/bin/env bash\n[[ \"$1\" == \"-c\" ]] || exit 1\n[[ \"$2\" == \"%u\" ]] && echo 0 || echo 755\n",
                "sleep": "#!/usr/bin/env bash\nexit 0\n",
                "flock": "#!/usr/bin/env bash\n[[ \"${TII_FAIL_LOCK:-0}\" == 1 ]] && exit 1\nexit 0\n",
                "mv": "#!/usr/bin/env bash\nif [[ \"$1\" == \"-Tf\" ]]; then shift; /bin/rm -f \"$2\"; exec /bin/mv -f \"$1\" \"$2\"; fi\nexec /bin/mv \"$@\"\n",
                "systemctl": "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$TII_LOG\"\nexit 0\n",
                "runuser": """#!/usr/bin/env bash
joined="$*"
if [[ "$joined" == *release_digest.py* ]]; then
  n=0; [[ -f "$TII_DIGEST_COUNTER" ]] && n=$(<"$TII_DIGEST_COUNTER")
  n=$((n+1)); echo "$n" > "$TII_DIGEST_COUNTER"
  target="${!#}"
  if [[ -n "${TII_TAMPER_TARGET:-}" && "$(readlink -f "$target")" == "$(readlink -f "$TII_TAMPER_TARGET")" && "$n" -ge 3 ]]; then
    echo sha256:tampered; exit 0
  fi
  [[ "$(readlink -f "$target")" == "$(readlink -f "$TII_PRIOR")" ]] && { echo sha256:prior; exit 0; }
  echo sha256:candidate; exit 0
fi
if [[ "$joined" == *tak_ili_inache.health* ]]; then
  n=0; [[ -f "$TII_HEALTH_COUNTER" ]] && n=$(<"$TII_HEALTH_COUNTER")
  n=$((n+1)); echo "$n" > "$TII_HEALTH_COUNTER"
  current=$(readlink -f "$TII_CURRENT")
  if [[ "${TII_RECOVERY:-0}" == 1 ]]; then
    if [[ "$n" == 1 ]]; then
      if [[ "${TII_BASELINE:-legacy-red}" == legacy-red ]]; then
        echo '{"data_ok":true,"delivery_ok":false,"liveness":{"ok":true,"reply_error_count":2,"last_reply_error_at":null}}'
      else
        echo '{"data_ok":true,"delivery_ok":true,"liveness":{"ok":true,"reply_error_count":2,"last_reply_error_at":"2026-09-07T09:00:00+00:00"}}'
      fi
      exit 0
    fi
    if [[ "$current" == "$(readlink -f "$TII_PRIOR")" ]]; then
      [[ "$joined" == *--require-delivery-health* ]] && exit 1
      exit 0
    fi
    if [[ "$joined" != *--require-delivery-health* ]]; then
      exit 0
    fi
    d=0; [[ -f "$TII_DELIVERY_COUNTER" ]] && d=$(<"$TII_DELIVERY_COUNTER")
    d=$((d+1)); echo "$d" > "$TII_DELIVERY_COUNTER"
    printf 'delivery %s\\n' "$d" >> "$TII_LOG"
    [[ "${TII_CANDIDATE_REPLY_ERROR:-0}" == 1 ]] && exit 1
    [[ "${TII_DELIVERY_READY_AFTER:-0}" -gt 0 && "$d" -ge "${TII_DELIVERY_READY_AFTER}" ]] && exit 0
    exit 1
  fi
fi
exit 0
""",
            }
            for name, content in commands.items():
                path = fake_bin / name
                path.write_text(content, encoding="utf-8")
                path.chmod(0o755)
            tamper_target = prior if tamper == "prior" else candidate if tamper == "candidate" else ""
            env = dict(
                os.environ,
                PATH=f"{fake_bin}:{os.environ['PATH']}",
                TII_LOG=str(log),
                TII_CURRENT=str(current),
                TII_PRIOR=str(prior),
                TII_RECOVERY="1" if recovery else "0",
                TII_BASELINE=baseline,
                TII_DELIVERY_READY_AFTER=str(delivery_ready_after),
                TII_CANDIDATE_REPLY_ERROR="1" if candidate_reply_error else "0",
                TII_FAIL_LOCK="1" if fail_lock else "0",
                TII_TAMPER_TARGET=str(tamper_target),
                TII_DIGEST_COUNTER=str(sandbox / "digest-count"),
                TII_HEALTH_COUNTER=str(sandbox / "health-count"),
                TII_DELIVERY_COUNTER=str(sandbox / "delivery-count"),
            )
            command = "activate-delivery-recovery" if recovery else "activate-transaction"
            result = subprocess.run([str(runner), command, "delivery-health-candidate"], text=True, capture_output=True, env=env, check=False)
            second = None
            if invoke_twice:
                second = subprocess.run([str(runner), command, "delivery-health-candidate"], text=True, capture_output=True, env=env, check=False)
            values = result, str(current.resolve()), str(prior.resolve()), str(candidate.resolve()), log.read_text(encoding="utf-8") if log.exists() else ""
            return (*values, second) if invoke_twice else values
