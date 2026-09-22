# SPDX-License-Identifier: MIT
"""Normalize one built sdist without changing its member bytes or modes."""

from __future__ import annotations

import copy
import gzip
import os
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None or not raw.isdigit():
        raise RuntimeError("SOURCE_DATE_EPOCH must be a non-negative integer")
    return int(raw)


def _single_sdist() -> Path:
    matches = sorted(DIST.glob("*.tar.gz"))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one sdist, found {matches}")
    return matches[0]


def main() -> int:
    epoch = _source_date_epoch()
    sdist = _single_sdist()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{sdist.name}.", suffix=".tmp", dir=DIST
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(sdist, "r:gz") as source, temporary.open("wb") as output:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=output, compresslevel=9, mtime=epoch
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT
                ) as target:
                    for member in sorted(source.getmembers(), key=lambda item: item.name):
                        if not (member.isdir() or member.isfile()):
                            raise RuntimeError(
                                f"sdist contains unsupported member type: {member.name}"
                            )
                        normalized = copy.copy(member)
                        normalized.uid = 0
                        normalized.gid = 0
                        normalized.uname = ""
                        normalized.gname = ""
                        normalized.mtime = epoch
                        normalized.pax_headers = {}
                        stream = source.extractfile(member) if member.isfile() else None
                        target.addfile(normalized, stream)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, sdist)
        directory = os.open(DIST, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
