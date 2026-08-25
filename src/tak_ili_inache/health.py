from __future__ import annotations

import argparse
import json

from .operations import health


def main() -> int:
    parser = argparse.ArgumentParser(description="Local health-check without Telegram API calls")
    parser.add_argument("--data-dir", default="data/runtime")
    parser.add_argument("--require-liveness", action="store_true", help="fail when the polling worker has not produced a recent heartbeat")
    parser.add_argument("--require-delivery-health", action="store_true", help="fail when the worker has recorded an outbound delivery error")
    args = parser.parse_args()
    report = health(args.data_dir, require_liveness=args.require_liveness, require_delivery_health=args.require_delivery_health)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
