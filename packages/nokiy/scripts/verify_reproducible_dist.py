# SPDX-License-Identifier: MIT
"""Rebuild once and prove release artifact digests are reproducible."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_digests() -> dict[str, str]:
    artifacts = sorted([*DIST.glob("*.whl"), *DIST.glob("*.tar.gz")])
    if len(artifacts) != 2:
        raise RuntimeError(f"expected one wheel and one sdist, found {artifacts}")
    return {path.name: _sha256(path) for path in artifacts}


def main() -> int:
    if not os.environ.get("SOURCE_DATE_EPOCH"):
        raise RuntimeError("SOURCE_DATE_EPOCH is required for reproducibility")
    first = _artifact_digests()
    subprocess.run([sys.executable, "scripts/clean_release_state.py"], cwd=ROOT, check=True)
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel"], cwd=ROOT, check=True
    )
    subprocess.run([sys.executable, "scripts/normalize_sdist.py"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "scripts/verify_dist.py"], cwd=ROOT, check=True)
    second = _artifact_digests()
    if second != first:
        raise RuntimeError(
            "release artifacts are not reproducible: "
            f"first={json.dumps(first, sort_keys=True)} "
            f"second={json.dumps(second, sort_keys=True)}"
        )
    print(
        json.dumps(
            {
                "reproducible": True,
                "source_date_epoch": os.environ["SOURCE_DATE_EPOCH"],
                "artifacts": second,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
