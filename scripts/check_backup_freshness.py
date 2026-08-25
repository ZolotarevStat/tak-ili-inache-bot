from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def _latest_snapshot_time(payload: str) -> datetime:
    snapshots = json.loads(payload)
    if not snapshots:
        raise ValueError("no tak-ili-inache backup snapshot exists")
    latest = max(item["time"] for item in snapshots)
    return datetime.fromisoformat(latest.replace("Z", "+00:00")).astimezone(timezone.utc)


def main() -> None:
    max_age_hours = int(os.environ.get("TAK_ILI_INACHE_BACKUP_MAX_AGE_HOURS", "30"))
    if max_age_hours < 1:
        raise ValueError("TAK_ILI_INACHE_BACKUP_MAX_AGE_HOURS must be positive")
    result = subprocess.run(
        ["restic", "snapshots", "--json", "--tag", "tak-ili-inache"],
        check=True,
        capture_output=True,
        text=True,
    )
    latest = _latest_snapshot_time(result.stdout)
    age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise RuntimeError(f"tak-ili-inache backup is stale: age_hours={age_hours:.1f}, max_age_hours={max_age_hours}")
    print(f"tak-ili-inache backup is fresh: age_hours={age_hours:.1f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"tak-ili-inache backup freshness check failed: {exc}", file=sys.stderr)
        raise
