#!/usr/bin/python3
"""Deterministic digest for the explicit infra-v6.3.7 install-source surface."""
from __future__ import annotations

import argparse
import hashlib
import stat
from pathlib import Path


FILES = (
    "tak-ili-inache-infra-admin",
    "tak-ili-inache-infra-admin.capability",
    "tak-ili-inache-infra-admin.sudoers",
    "tak-ili-inache-infra-upgrade",
    "tii_infra_ops.py",
    "tii_infra_digest.py",
    "tak-ili-inache-backup-run",
    "tak-ili-inache-backup-freshness-run",
    "tak-ili-inache-alloy-run",
    "tak-ili-inache-metrics-snapshot",
    "tak-ili-inache-backup.service",
    "tak-ili-inache-backup.timer",
    "tak-ili-inache-backup-freshness.service",
    "tak-ili-inache-backup-freshness.timer",
    "tak-ili-inache-metrics-snapshot.service",
    "tak-ili-inache-metrics-snapshot.timer",
    "tak-ili-inache-alloy.service",
    "tak-ili-inache-alloy.config.alloy",
)


def package_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in FILES:
        path = root / relative
        mode = path.lstat().st_mode
        if path.is_symlink() or not stat.S_ISREG(mode):
            raise ValueError(f"unsafe infra source: {relative}")
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(str(stat.S_IMODE(mode)).encode("ascii") + b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    print(package_digest(args.root))


if __name__ == "__main__":
    main()
