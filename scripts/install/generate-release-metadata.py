#!/usr/bin/env python3
"""Generate install metadata JSON for GitHub Pages."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_payload(*, version: str, wheel: Path, tag: str | None = None) -> dict:
    tag_name = tag or f"v{version}"
    filename = wheel.name
    return {
        "schema": "subactor.shell.install/v1",
        "version": version,
        "tag": tag_name,
        "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "assets": {
            "wheel": {
                "filename": filename,
                "url": (
                    f"https://github.com/subactor/shell/releases/download/"
                    f"{tag_name}/{filename}"
                ),
                "sha256": sha256_file(wheel),
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, e.g. 0.2.2")
    parser.add_argument("--wheel", required=True, type=Path, help="Path to wheel file")
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output JSON path (e.g. docs/channels/latest.json)",
    )
    parser.add_argument("--tag", help="Git tag name override, e.g. v0.2.2")
    args = parser.parse_args()

    payload = build_payload(version=args.version, wheel=args.wheel, tag=args.tag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
