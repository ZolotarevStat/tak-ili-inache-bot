from __future__ import annotations

import argparse
from tak_ili_inache.operations import create_backup, restore_backup


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_subparsers(dest="action", required=True)
    backup = action.add_parser("create")
    backup.add_argument("data_dir")
    backup.add_argument("archive")
    restore = action.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("data_dir")
    args = parser.parse_args()
    path = create_backup(args.data_dir, args.archive) if args.action == "create" else restore_backup(args.archive, args.data_dir)
    print(path)


if __name__ == "__main__":
    main()
