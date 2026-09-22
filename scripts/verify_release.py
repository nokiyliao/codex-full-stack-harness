"""Verify public source hashes without models or service installation."""
import hashlib
import json
from pathlib import Path


def verify(root: Path) -> int:
    manifest = json.loads((root / "releases/v9.json").read_text())
    count = 0
    for component, entry in manifest["components"].items():
        for name, expected in entry["files"].items():
            path = root / component / name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Invalid source path: {component}/{name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Source hash mismatch: {component}/{name}")
            count += 1
    return count


if __name__ == "__main__":
    print(f"Verified {verify(Path(__file__).resolve().parents[1])} source files for Nokiy v9")
