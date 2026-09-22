# SPDX-License-Identifier: MIT
"""Remove only reproducible local packaging state."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    targets = [
        ROOT / "dist",
        ROOT / "build",
        *sorted((ROOT / "src").glob("*.egg-info")),
    ]
    for target in targets:
        if target.is_symlink():
            raise RuntimeError(f"refusing to clean symlinked release state: {target}")
        if target.exists() and not target.is_dir():
            raise RuntimeError(f"release state target is not a directory: {target}")
        if target.exists():
            shutil.rmtree(target)
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"release state cleanup did not remove: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
