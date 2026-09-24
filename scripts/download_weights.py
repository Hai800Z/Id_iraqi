#!/usr/bin/env python3
"""Download the model weights from a release and verify their SHA-256 checksums.

    python scripts/download_weights.py --base-url https://github.com/<owner>/<repo>/releases/download/<tag>
    python scripts/download_weights.py --verify

The expected files and checksums are listed in models/SHA256SUMS.
Only the standard library is used, so this runs before `pip install`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKSUMS = ROOT / "models" / "SHA256SUMS"
DEFAULT_DEST = ROOT / "models" / "weights"


def read_checksums(path: Path) -> dict[str, str]:
    """Parse ``sha256sum`` output: ``<digest> [*]<file name>`` per line."""
    expected = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split(maxsplit=1)
        expected[name.lstrip("*")] = digest.lower()
    return expected


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:  # noqa: S310 (scheme checked in main)
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {target.name}: {done * 100 // total}%", end="", flush=True)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and verify the model weights.")
    parser.add_argument("--base-url", default=os.getenv("WEIGHTS_BASE_URL"),
                        help="URL prefix of the release files (or set WEIGHTS_BASE_URL)")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="target folder (default: models/weights)")
    parser.add_argument("--verify", action="store_true", help="only verify the files already present")
    parser.add_argument("--force", action="store_true", help="download even when a valid file exists")
    args = parser.parse_args()

    if args.base_url and not args.base_url.startswith("https://"):
        print("--base-url must start with https://", file=sys.stderr)
        return 1

    expected = read_checksums(CHECKSUMS)
    args.dest.mkdir(parents=True, exist_ok=True)
    failures = 0

    for name, digest in expected.items():
        target = args.dest / name
        if target.is_file() and not args.force:
            if sha256(target) == digest:
                print(f"OK        {name}")
                continue
            print(f"MISMATCH  {name}: checksum differs from models/SHA256SUMS")
            if args.verify:
                failures += 1
                continue
        elif args.verify:
            print(f"MISSING   {name}")
            failures += 1
            continue

        if not args.base_url:
            print("Set --base-url or WEIGHTS_BASE_URL to download the weights.", file=sys.stderr)
            return 1

        url = f"{args.base_url.rstrip('/')}/{name}"
        partial = target.with_name(name + ".part")
        print(f"Download  {url}")
        try:
            download(url, partial)
        except OSError as exc:
            print(f"FAILED    {name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        if sha256(partial) != digest:
            print(f"FAILED    {name}: checksum mismatch, file discarded", file=sys.stderr)
            partial.unlink()
            failures += 1
            continue
        os.replace(partial, target)
        print(f"OK        {name}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
