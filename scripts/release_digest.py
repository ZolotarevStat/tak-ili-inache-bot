"""Print a deterministic SHA-256 digest for a deployable release tree."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


RUNTIME_FILES = {Path("pyproject.toml"), Path("Dockerfile")}
RUNTIME_DIRECTORIES = {"src/tak_ili_inache", "deploy"}


def included_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative in RUNTIME_FILES:
            files.append(path)
            continue
        if not any(relative.as_posix().startswith(f"{directory}/") for directory in RUNTIME_DIRECTORIES):
            continue
        if path.suffix == ".pyc" or any(part.endswith(".egg-info") or part == "__pycache__" for part in relative.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def release_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in included_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(f"sha256:{release_digest(args.root.resolve())}")


if __name__ == "__main__":
    main()
