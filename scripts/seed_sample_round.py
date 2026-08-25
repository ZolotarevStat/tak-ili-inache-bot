"""Seed local runtime storage with the sanitised sample round; no Telegram access."""
from __future__ import annotations

import argparse
from pathlib import Path

from tak_ili_inache.csv_repository import CsvRepository
from tak_ili_inache.fixtures import import_fixtures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data/runtime")
    parser.add_argument("--fixtures", default="data/fixtures_sample.csv")
    args = parser.parse_args()
    round_ = import_fixtures(Path(args.fixtures))
    CsvRepository(args.data_dir).save_round(round_)
    print(f"Seeded {round_.round_id}: {len(round_.fixtures)} fixtures, deadline {round_.deadline_msk.isoformat()}")


if __name__ == "__main__":
    main()
