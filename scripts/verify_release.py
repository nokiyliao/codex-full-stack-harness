"""Verify the current public source snapshot without models or service installation."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


IGNORED = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", "target", "dist", "build"}


def source_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name)
    if not name or parts.is_absolute() or "\\" in name or any(p in {".", ".."} for p in name.split("/")):
        raise ValueError(f"Invalid source path: {name}")
    path = root
    for part in parts.parts:
        path /= part
        if path.is_symlink():
            raise ValueError(f"Symlink source path: {name}")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Missing source path: {name}")
    return path


def verify(root: Path) -> int:
    current = json.loads((root / "releases/CURRENT.json").read_text())
    name = current["manifest"]
    if not isinstance(name, str) or Path(name).name != name or name.startswith("."):
        raise ValueError("Invalid current manifest pointer")
    manifest = json.loads(source_path(root, f"releases/{name}").read_text())
    count = 0
    for component, entry in manifest["components"].items():
        expected = entry["files"]
        for name, digest in expected.items():
            path = source_path(root, f"{component}/{name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"Source hash mismatch: {component}/{name}")
            mode = entry.get("modes", {}).get(name)
            if mode is not None and bool(path.stat().st_mode & 0o111) != (mode == "100755"):
                raise ValueError(f"Executable bit mismatch: {component}/{name}")
            count += 1
        directory = root / component
        for parent, directories, files in os.walk(directory, followlinks=False):
            directories[:] = [d for d in directories if d not in IGNORED]
            if any((Path(parent) / d).is_symlink() for d in directories):
                raise ValueError(f"Symlink source directory: {component}")
            for filename in files:
                if filename.endswith(".pyc"):
                    continue
                name = (Path(parent) / filename).relative_to(directory).as_posix()
                if name not in expected:
                    raise ValueError(f"Unlisted source file: {component}/{name}")
    for name, digest in manifest.get("publication_files", {}).items():
        if hashlib.sha256(source_path(root, name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Publication hash mismatch: {name}")
        count += 1
    return count


if __name__ == "__main__":
    print(f"Verified {verify(Path(__file__).resolve().parents[1])} current source files")
