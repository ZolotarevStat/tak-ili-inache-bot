#!/usr/bin/python3
"""Least-privilege, non-eval operations for the infra-v6.3.7 package.

This file deliberately never invokes a shell and never sources an env file.
Secret configuration is parsed as a small data format, validated, and passed
only to the exact child process that requires it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import grp
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


CAPABILITY: Final = "tak-ili-inache-infra-admin:v6.3.7-operational-status"
BACKUP_ENV: Final = Path("/etc/tak-ili-inache-backup.env")
RESTIC_PASSWORD: Final = Path("/etc/tak-ili-inache-restic-password")
GRAFANA_ENV: Final = Path("/etc/tak-ili-inache-grafana.env")
DATA_DIR: Final = Path("/var/lib/tak-ili-inache")
METRICS_DIR: Final = Path("/var/lib/tak-ili-inache-metrics")
ACCEPTANCE_MARKER: Final = METRICS_DIR / "backup_restore_accepted"
FRESHNESS_MARKER: Final = METRICS_DIR / "backup_freshness_epoch"
CURRENT: Final = Path("/opt/tak-ili-inache/current")
RESTIC: Final = "/usr/bin/restic"
SYSTEMCTL: Final = "/usr/bin/systemctl"
RUNUSER: Final = "/usr/sbin/runuser"
ALLOY: Final = "/usr/bin/alloy"
SYSTEMD_ENABLED_RETURN_CODES: Final = {
    "enabled": 0,
    "enabled-runtime": 0,
    "linked": 0,
    "linked-runtime": 0,
    "alias": 0,
    "generated": 0,
    "transient": 0,
    "static": 0,
    "indirect": 0,
    "disabled": 1,
    "not-found": 1,
}
ALLOWED_INACTIVE_ENABLEMENT_STATES: Final = frozenset({"static", "indirect", "disabled", "not-found"})
BLOCKED_ENABLEMENT_STATES: Final = frozenset({"enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "generated", "transient"})

BACKUP_KEYS: Final = frozenset(
    {
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "RESTIC_CACHE_DIR",
        "TAK_ILI_INACHE_BACKUP_MAX_AGE_HOURS",
        "TAK_ILI_INACHE_BACKUP_KEEP_DAILY",
        "TAK_ILI_INACHE_BACKUP_KEEP_WEEKLY",
        "TAK_ILI_INACHE_BACKUP_KEEP_MONTHLY",
    }
)
GRAFANA_KEYS: Final = frozenset(
    {
        "GRAFANA_CLOUD_PROMETHEUS_URL",
        "GRAFANA_CLOUD_PROMETHEUS_USERNAME",
        "GRAFANA_CLOUD_METRICS_API_KEY",
    }
)
REQUIRED_BACKUP: Final = BACKUP_KEYS
REQUIRED_GRAFANA: Final = GRAFANA_KEYS
LINE_RE: Final = re.compile(r"^([A-Z][A-Z0-9_]*)=([^\s\x00-\x1f]+)$")
FORBIDDEN_VALUE_CHARS: Final = frozenset("$`;&|<>'\"(){}[]!*?\\")
S3_REPOSITORY_RE: Final = re.compile(r"^s3:https://[A-Za-z0-9.-]+/[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._/-]*)?$")
GRAFANA_URL_RE: Final = re.compile(r"^https://[A-Za-z0-9.-]+/api/(?:prom/push|v1/write)$")


class ConfigError(ValueError):
    """Safe error: do not include the offending value in the message."""


def _secret_file(path: Path, *, require_nonempty: bool = True) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ConfigError("required protected configuration file is missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ConfigError("protected configuration file type is invalid")
    if path.stat().st_uid != 0 or path.stat().st_gid != 0 or stat.S_IMODE(mode) != 0o600:
        raise ConfigError("protected configuration ownership or mode is invalid")
    if require_nonempty and path.stat().st_size == 0:
        raise ConfigError("protected configuration file is empty")


def _root_marker(path: Path) -> None:
    """Acceptance markers are root-owned regular files, never secret inputs."""
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ConfigError("root acceptance marker type is invalid")
    if path.stat().st_uid != 0 or path.stat().st_gid != 0 or stat.S_IMODE(mode) != 0o640 or path.stat().st_size == 0:
        raise ConfigError("root acceptance marker metadata is invalid")


def parse_protected_env(path: Path, allowed: frozenset[str], required: frozenset[str]) -> dict[str, str]:
    """Parse a restricted KEY=value file without shell expansion or interpolation."""
    _secret_file(path)
    values: dict[str, str] = {}
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ConfigError("protected configuration is not UTF-8") from exc
    for row in rows:
        if not row or row.startswith("#"):
            continue
        match = LINE_RE.fullmatch(row)
        if match is None:
            raise ConfigError("protected configuration has malformed line")
        key, value = match.groups()
        if key not in allowed:
            raise ConfigError("protected configuration has unknown key")
        if key in values:
            raise ConfigError("protected configuration has duplicate key")
        if any(char in FORBIDDEN_VALUE_CHARS for char in value):
            raise ConfigError("protected configuration contains forbidden metacharacter")
        values[key] = value
    if values.keys() != required:
        raise ConfigError("protected configuration key set is incomplete or unexpected")
    return values


def backup_config() -> dict[str, str]:
    values = parse_protected_env(BACKUP_ENV, BACKUP_KEYS, REQUIRED_BACKUP)
    _secret_file(RESTIC_PASSWORD)
    if values["RESTIC_PASSWORD_FILE"] != str(RESTIC_PASSWORD):
        raise ConfigError("RESTIC_PASSWORD_FILE is not the dedicated protected path")
    if values["RESTIC_CACHE_DIR"] != "/var/cache/tak-ili-inache-restic":
        raise ConfigError("RESTIC_CACHE_DIR is not the dedicated cache path")
    if not S3_REPOSITORY_RE.fullmatch(values["RESTIC_REPOSITORY"]):
        raise ConfigError("RESTIC_REPOSITORY is outside the approved S3 HTTPS form")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", values["AWS_ACCESS_KEY_ID"]):
        raise ConfigError("AWS access key format is invalid")
    if not re.fullmatch(r"[A-Za-z0-9+/=._-]+", values["AWS_SECRET_ACCESS_KEY"]):
        raise ConfigError("AWS secret key format is invalid")
    if not re.fullmatch(r"[a-z0-9-]+", values["AWS_DEFAULT_REGION"]):
        raise ConfigError("AWS region format is invalid")
    for key in (
        "TAK_ILI_INACHE_BACKUP_MAX_AGE_HOURS",
        "TAK_ILI_INACHE_BACKUP_KEEP_DAILY",
        "TAK_ILI_INACHE_BACKUP_KEEP_WEEKLY",
        "TAK_ILI_INACHE_BACKUP_KEEP_MONTHLY",
    ):
        if not re.fullmatch(r"[1-9][0-9]{0,3}", values[key]):
            raise ConfigError("backup numeric policy is invalid")
    return values


def grafana_config() -> dict[str, str]:
    values = parse_protected_env(GRAFANA_ENV, GRAFANA_KEYS, REQUIRED_GRAFANA)
    if not GRAFANA_URL_RE.fullmatch(values["GRAFANA_CLOUD_PROMETHEUS_URL"]):
        raise ConfigError("Grafana remote-write URL is invalid")
    if not re.fullmatch(r"[0-9]+", values["GRAFANA_CLOUD_PROMETHEUS_USERNAME"]):
        raise ConfigError("Grafana username is invalid")
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", values["GRAFANA_CLOUD_METRICS_API_KEY"]):
        raise ConfigError("Grafana API key format is invalid")
    return values


def clean_env(values: dict[str, str]) -> dict[str, str]:
    """Only exact config values reach a subprocess; caller environment is discarded."""
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", **values}


def run(command: list[str], *, values: dict[str, str] | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        env=clean_env(values or {}),
        text=True,
        capture_output=capture,
    )


def ensure_metrics_dir() -> None:
    METRICS_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(METRICS_DIR, 0, 0)
    os.chmod(METRICS_DIR, 0o750)


def atomic_marker(path: Path, value: str) -> None:
    ensure_metrics_dir()
    fd, temporary = tempfile.mkstemp(prefix=".tii.", dir=METRICS_DIR)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def latest_snapshot_epoch(values: dict[str, str]) -> int:
    output = run([RESTIC, "snapshots", "--json", "--tag", "tak-ili-inache"], values=values, capture=True).stdout
    snapshots = json.loads(output)
    if not isinstance(snapshots, list) or not snapshots:
        raise RuntimeError("no tagged backup snapshot exists")
    timestamps = [item.get("time") for item in snapshots if isinstance(item, dict)]
    parsed = [datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc) for value in timestamps]
    if not parsed:
        raise RuntimeError("backup snapshots have no usable timestamp")
    return int(max(parsed).timestamp())


def backup_service() -> None:
    values = backup_config()
    if not DATA_DIR.is_dir():
        raise RuntimeError("runtime data directory is missing")
    with Path("/run/tak-ili-inache-backup.lock").open("w", encoding="ascii") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another backup is already running") from exc
        repository_lock_path = DATA_DIR / ".repository.lock"
        # The backup unit intentionally sees DATA_DIR read-only under
        # ProtectSystem=strict. Linux flock supports an exclusive advisory lock
        # on a read-only descriptor, so no write permission is needed here.
        descriptor = os.open(repository_lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("repository lock is not a regular file")
            # CsvRepository uses the same exclusive flock for every multi-file
            # commit. Holding it for restic's scan gives the snapshot one
            # coherent repository revision instead of a mix of two commits.
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            run([RESTIC, "cat", "config"], values=values)
            run([RESTIC, "backup", "--tag", "tak-ili-inache", "--exclude", ".repository.lock", "--exclude", "*.png", "--one-file-system", str(DATA_DIR)], values=values)
        finally:
            os.close(descriptor)
        run(
            [RESTIC, "forget", "--prune", "--tag", "tak-ili-inache", "--keep-daily", values["TAK_ILI_INACHE_BACKUP_KEEP_DAILY"], "--keep-weekly", values["TAK_ILI_INACHE_BACKUP_KEEP_WEEKLY"], "--keep-monthly", values["TAK_ILI_INACHE_BACKUP_KEEP_MONTHLY"]],
            values=values,
        )


def freshness_service() -> None:
    values = backup_config()
    latest = latest_snapshot_epoch(values)
    max_age = int(values["TAK_ILI_INACHE_BACKUP_MAX_AGE_HOURS"]) * 3600
    if int(time.time()) - latest > max_age:
        raise RuntimeError("external backup is stale")
    atomic_marker(FRESHNESS_MARKER, str(latest))


def validate_restore_tree(root: Path) -> Path:
    """Reject links and device files before any ownership change or health check."""
    base = root.resolve(strict=True)
    for entry in [base, *base.rglob("*")]:
        mode = entry.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise RuntimeError("isolated restore contains an unsafe file type")
        resolved = entry.resolve(strict=True)
        if not resolved.is_relative_to(base):
            raise RuntimeError("isolated restore escapes its canonical root")
    data = base / "var/lib/tak-ili-inache"
    if not data.is_dir() or data.is_symlink() or not data.resolve(strict=True).is_relative_to(base):
        raise RuntimeError("isolated restore has no contained runtime data")
    return data


def restore_verify() -> None:
    values = backup_config()
    restore_root = Path(tempfile.mkdtemp(prefix="tak-ili-inache-restore.", dir="/var/tmp"))
    try:
        os.chmod(restore_root, 0o700)
        run([RESTIC, "restore", "latest", "--target", str(restore_root)], values=values)
        restored_data = validate_restore_tree(restore_root)
        worker = pwd.getpwnam("takiliinache")
        worker_group = grp.getgrnam("takiliinache")
        # The health process must be able to traverse the mkdtemp parent.
        # Keep it 0700 and transfer ownership only after the restored tree has
        # passed the root-side symlink/special-file containment checks.
        os.chown(restore_root, worker.pw_uid, worker_group.gr_gid)
        for entry in [restored_data, *restored_data.rglob("*")]:
            os.chown(entry, worker.pw_uid, worker_group.gr_gid)
        run([RUNUSER, "-u", "takiliinache", "--", str(CURRENT / ".venv/bin/python"), "-m", "tak_ili_inache.health", "--data-dir", str(restored_data)], values={})
        atomic_marker(ACCEPTANCE_MARKER, str(int(time.time())))
    finally:
        shutil.rmtree(restore_root)


class SystemdStateError(RuntimeError):
    pass


def _systemd_query(verb: str, unit: str) -> tuple[str, int]:
    try:
        result = subprocess.run(
            [SYSTEMCTL, verb, unit],
            check=False,
            text=True,
            capture_output=True,
            env=clean_env({}),
        )
    except OSError as exc:
        raise SystemdStateError(f"systemd {verb} execution failed for {unit}") from exc
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stderr:
        raise SystemdStateError(f"systemd {verb} failed for {unit}")
    if not stdout:
        raise SystemdStateError(f"systemd {verb} returned empty state for {unit}")
    return stdout, result.returncode


def systemd_enabled_state(unit: str) -> str:
    state, returncode = _systemd_query("is-enabled", unit)
    expected_returncode = SYSTEMD_ENABLED_RETURN_CODES.get(state)
    if expected_returncode is None:
        raise SystemdStateError(f"systemd is-enabled returned unknown state {state!r} for {unit}")
    if returncode != expected_returncode:
        raise SystemdStateError(f"systemd is-enabled returned inconsistent state for {unit}")
    return state


def systemd_inactive_state(unit: str) -> str:
    state, returncode = _systemd_query("is-active", unit)
    if state != "inactive" or returncode != 3:
        raise SystemdStateError(f"systemd is-active did not confirm inactive for {unit}")
    return state


def systemd_runtime_state(unit: str) -> str:
    """Return a stable active/inactive state for operational readout."""
    state, returncode = _systemd_query("is-active", unit)
    expected = {"active": 0, "inactive": 3}.get(state)
    if expected is None or returncode != expected:
        raise SystemdStateError(f"systemd is-active returned unsafe state {state!r} for {unit}")
    return state


def unit_state(unit: str) -> tuple[str, str]:
    return systemd_enabled_state(unit), systemd_inactive_state(unit)


def assert_disabled_inactive(units: tuple[str, ...]) -> None:
    for unit in units:
        enabled, _ = unit_state(unit)
        if enabled in BLOCKED_ENABLEMENT_STATES:
            raise RuntimeError(f"enable compensation did not disable {unit}: {enabled}")
        if enabled not in ALLOWED_INACTIVE_ENABLEMENT_STATES:
            raise SystemdStateError(f"systemd is-enabled returned unsafe state {enabled!r} for {unit}")


def enable_units_transaction(units: tuple[str, ...]) -> None:
    """Enable/start exactly these units or compensate all of them on failure."""
    try:
        run([SYSTEMCTL, "enable", "--now", *units])
    except BaseException as primary:
        compensation_errors: list[BaseException] = []
        for unit in units:
            try:
                run([SYSTEMCTL, "disable", "--now", unit])
            except BaseException as exc:  # continue to attempt every exact unit
                compensation_errors.append(exc)
        try:
            assert_disabled_inactive(units)
        except BaseException as compensation_failure:
            raise RuntimeError("enable compensation failed") from primary
        if compensation_errors:
            raise RuntimeError("enable compensation command failed") from primary
        raise primary


BACKUP_ENABLE_UNITS: Final = ("tak-ili-inache-backup.timer", "tak-ili-inache-backup-freshness.timer")
ALLOY_ENABLE_UNITS: Final = ("tak-ili-inache-metrics-snapshot.timer", "tak-ili-inache-alloy.service")


def enable_backup() -> None:
    backup_config()
    _root_marker(ACCEPTANCE_MARKER)
    enable_units_transaction(BACKUP_ENABLE_UNITS)
    print("backup_timers=enabled")


def enable_alloy() -> None:
    grafana_config()
    if not Path(ALLOY).is_file() or not os.access(ALLOY, os.X_OK):
        raise RuntimeError("Grafana Alloy package is not installed; unit remains disabled")
    enable_units_transaction(ALLOY_ENABLE_UNITS)
    print("alloy_metrics=enabled")


def status() -> None:
    print(f"infra_capability={CAPABILITY}")
    for path in (BACKUP_ENV, RESTIC_PASSWORD, GRAFANA_ENV):
        try:
            _secret_file(path, require_nonempty=False)
            print(f"{path} present mode=600 owner=root:root")
        except ConfigError:
            print(f"{path} absent-or-invalid")
    for unit in ("tak-ili-inache-backup.timer", "tak-ili-inache-backup-freshness.timer", "tak-ili-inache-metrics-snapshot.timer", "tak-ili-inache-alloy.service"):
        enabled, active = systemd_enabled_state(unit), systemd_runtime_state(unit)
        print(f"{unit} enabled={enabled} active={active}")
    print("alloy_binary=present" if Path(ALLOY).is_file() and os.access(ALLOY, os.X_OK) else "alloy_binary=absent")


def command(name: str) -> None:
    if name == "capability":
        print(f"infra_capability={CAPABILITY}")
    elif name == "status":
        status()
    elif name == "backup-init":
        values = backup_config()
        try:
            run([RESTIC, "cat", "config"], values=values)
            print("backup_repository=already-initialized")
        except subprocess.CalledProcessError:
            run([RESTIC, "init"], values=values)
            run([RESTIC, "cat", "config"], values=values)
            print("backup_repository=initialized")
    elif name == "backup-run":
        backup_config()
        run([SYSTEMCTL, "start", "tak-ili-inache-backup.service"])
        run([SYSTEMCTL, "start", "tak-ili-inache-backup-freshness.service"])
        print("backup_run=passed")
    elif name == "backup-restore-verify":
        restore_verify()
        print("backup_restore_health=passed")
    elif name == "backup-enable":
        enable_backup()
    elif name == "alloy-enable":
        enable_alloy()
    elif name == "alloy-service":
        values = grafana_config()
        run([ALLOY, "run", "--server.http.listen-addr=127.0.0.1:12345", "--storage.path=/var/lib/tak-ili-inache-alloy", "/etc/tak-ili-inache/alloy/config.alloy"], values=values)
    elif name == "backup-service":
        backup_service()
    elif name == "freshness-service":
        freshness_service()
    else:
        raise RuntimeError("unsupported infrastructure verb")


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("verb", choices=("capability", "status", "backup-init", "backup-run", "backup-restore-verify", "backup-enable", "alloy-enable", "alloy-service", "backup-service", "freshness-service"))
    args = parser.parse_args()
    command(args.verb)


if __name__ == "__main__":
    try:
        main()
    except (ConfigError, RuntimeError, OSError, subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as exc:
        print(f"tak-ili-inache infra operation failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
