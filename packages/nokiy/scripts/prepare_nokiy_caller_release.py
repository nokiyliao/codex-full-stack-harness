#!/usr/bin/env python3
"""Parent-only TASK.json adapter to the audited caller/Rust packager/CAS installer.

No deployment authority, lease clearance, retry, installer or command language is
provided here. Templates execute only after their explicit identities are checked.
Optional runtime_build is a {path, sha256} identity of a parent-produced receipt;
its argv is evidence only and is never executed by this adapter.
That receipt may explicitly bind existing command schema or prompt catalog replacements.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import types
from typing import Any

SCHEMA = "nokiy_caller_release_task_v1"
PACKAGE = "codex_collaboration_harness"
SITE = Path("caller/lib/python3.11/site-packages")
SCRIPT = "scripts/prepare_nokiy_caller_release.py"
LEGACY_PATH = Path("/Volumes/NOKIY-TB5/UTM/runtime_repros/nokiy-v64-same-model-long-20260930-v1/caller-source-convergence-v1/delivery.py")
LEGACY_SHA256 = "201bd00f95e756a0c015bca0de0ce15b4f24d101a2b0ea2d884706cc7a4e0ae7"
PHASES = ("preflight", "apply", "verify")
METADATA = ("caller-binding.json", "runtime-image.json", "source-provenance.json")
GUIDANCE = ("pyproject.toml", "skills/nokiy/SKILL.md", "skills/nokiy/references/invocation.md")
RUNTIME_BINS = ("tura_exec", "tura_runtime", "tura_router", "tura_session_db")
HEX = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
THREAD = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
LIMIT = 128 * 1024
EVIDENCE_LIMIT = 1024 * 1024
NATIVE_CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
PAIRED_WIRE_TEST = "test_verifier_parent.PairedVerifierWireTests.test_candidate_roundtrip"


class PreparationError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(code + (": " + detail if detail else ""))


def require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise PreparationError(code, detail)


def canonical(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    return digest.hexdigest()


def fields(value: Any, names: tuple[str, ...], code: str) -> None:
    require(isinstance(value, dict) and set(value) == set(names), code)


def digest(value: Any) -> str:
    require(isinstance(value, str) and HEX.fullmatch(value) is not None, "SHA256_INVALID")
    return value


def absolute(value: Any, *, missing: bool = False) -> Path:
    require(isinstance(value, str) and "\0" not in value and "\\" not in value,
            "PATH_INVALID")
    path = Path(value)
    require(path.is_absolute() and str(path) == value and ".." not in path.parts
            and ".git" not in path.parts, "PATH_INVALID", value)
    require(path.resolve(strict=not missing) == path, "CANONICAL_PATH_REQUIRED", value)
    return path


def regular(path: Path) -> None:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), "REGULAR_FILE_REQUIRED", str(path))


def file_identity(value: Any) -> Path:
    require(isinstance(value, dict) and set(value) in ({"path", "sha256"},
            {"path", "sha256", "realpath"}), "FILE_IDENTITY_INVALID")
    path = Path(value["path"]) if isinstance(value["path"], str) else Path()
    require(path.is_absolute() and str(path) == value["path"] and ".." not in path.parts
            and ".git" not in path.parts, "PATH_INVALID")
    resolved = path.resolve(strict=True)
    require((resolved == path and "realpath" not in value)
            or value.get("realpath") == str(resolved), "RESOLVED_IDENTITY_REQUIRED", str(path))
    regular(resolved)
    require(sha(path) == digest(value["sha256"]), "INPUT_DRIFT", str(path))
    return path


def identity(path: Path) -> dict[str, str]:
    value = {"path": str(path), "sha256": sha(path)}
    if path.resolve(strict=True) != path:
        value["realpath"] = str(path.resolve(strict=True))
    return value


def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        require(key not in value, "JSON_DUPLICATE_KEY", key)
        value[key] = item
    return value


def document(path: Path, *, limit: int = LIMIT) -> dict[str, Any]:
    regular(path)
    require(path.stat().st_size <= limit, "JSON_TOO_LARGE", str(path))
    def invalid_constant(_: str) -> None:
        raise PreparationError("JSON_NONFINITE")
    value = json.loads(path.read_bytes(), object_pairs_hook=unique_pairs,
                       parse_constant=invalid_constant)
    require(isinstance(value, dict), "JSON_OBJECT_REQUIRED", str(path))
    return value


def save(path: Path, value: Any) -> None:
    # Preserve even an interrupted write. Never delete, truncate, resume or retry.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def relative(value: Any) -> str:
    require(isinstance(value, str) and "\\" not in value and "\0" not in value,
            "RELATIVE_PATH_INVALID")
    path = PurePosixPath(value)
    require(bool(value) and not path.is_absolute() and str(path) == value
            and all(part not in ("..", ".git") and not part.startswith(".") for part in path.parts),
            "RELATIVE_PATH_INVALID", value)
    return value


def package_bytes(root: Path) -> dict[str, str]:
    """Complete effective tree; bytecode caches are neither copied nor executed."""
    require(root.is_dir() and root.resolve(strict=True) == root, "TREE_INVALID", str(root))
    values: dict[str, str] = {}
    def failed_walk(error: OSError) -> None:
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed_walk):
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in dirs + sorted(files):
            path = Path(directory) / name
            require(not path.is_symlink(), "TREE_SYMLINK", str(path))
            if name in files:
                regular(path)
                values[str(path.relative_to(root))] = sha(path)
    return values


def tree_sha256(root: Path) -> str:
    return canonical(package_bytes(root))


def validate_agent_prompt_updates(old: Path, updates: Any) -> dict[str, Any]:
    """Validate exact existing prompt identities; this does not authorize or stage them."""
    require(isinstance(updates, dict) and 0 < len(updates) <= 32, "AGENT_PROMPT_UPDATES_INVALID")
    artifacts = document(old / "runtime-image.json")["artifacts"]
    destinations: set[Path] = set()
    for name, pin in updates.items():
        require(isinstance(name, str) and name in artifacts, "AGENT_PROMPT_SCOPE_INVALID")
        fields(pin, ("path", "sha256", "previous_sha256"), "AGENT_PROMPT_IDENTITY_INVALID")
        prior = absolute(artifacts[name]["path"])
        source = absolute(pin["path"])
        regular(prior)
        regular(source)
        require(prior.is_relative_to(old / "runtime"), "AGENT_PROMPT_SCOPE_INVALID")
        target = prior.relative_to(old / "runtime")
        require(re.fullmatch(r"agents/src/[A-Za-z0-9_-]+/prompt\.md", str(target)) is not None
                and source.parts[-len(target.parts):] == target.parts
                and not source.is_relative_to(old), "AGENT_PROMPT_SCOPE_INVALID")
        require(prior not in destinations, "AGENT_PROMPT_DESTINATION_COLLISION")
        destinations.add(prior)
        require(digest(pin["previous_sha256"]) == artifacts[name]["sha256"] == sha(prior),
                "AGENT_PROMPT_PREIMAGE_DRIFT")
        require(sha(source) == digest(pin["sha256"]), "AGENT_PROMPT_SOURCE_DRIFT")
        require(0 < source.stat().st_size <= LIMIT, "AGENT_PROMPT_CONTENT_INVALID")
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise PreparationError("AGENT_PROMPT_CONTENT_INVALID") from exc
        require(bool(text.strip()) and "\0" not in text, "AGENT_PROMPT_CONTENT_INVALID")
    return updates


@dataclass(frozen=True)
class Task:
    path: Path
    manifest_sha256: str
    data: dict[str, Any]
    repo: Path
    root: Path
    old: Path
    new: Path

    @property
    def here(self) -> Path:
        return self.root / "deployment-evidence"

    @property
    def out(self) -> Path:
        return self.here / "cutover"

    @classmethod
    def load(cls, manifest: str, approved: str) -> Task:
        path = absolute(manifest)
        regular(path)
        require(sha(path) == digest(approved), "MANIFEST_SHA_MISMATCH")
        data = document(path)
        fields(data, ("schema_version", "native_thread_id", "repo", "artifact_root", "environment",
                      "installed", "candidate", "action_id", "modules", "tests", "postimages",
                      "adapter", "templates", "interpreters", "runtime_dependencies", "config",
                      "authorization_ref", "expires_at") +
                     tuple(key for key in ("runtime_build", "agent_prompt_updates") if key in data),
               "MANIFEST_FIELDS_INVALID")
        require(data["schema_version"] == SCHEMA and path.name == "TASK.json", "MANIFEST_SCHEMA_INVALID")
        fields(data["installed"], ("release", "entry", "metadata", "package_sha256"), "INSTALLED_INVALID")
        fields(data["candidate"], ("release", "version", "build_id"), "CANDIDATE_INVALID")
        repo, root = absolute(data["repo"]), absolute(data["artifact_root"])
        old, new = absolute(data["installed"]["release"]), absolute(data["candidate"]["release"], missing=True)
        require(repo.is_dir() and root.is_dir() and old.is_dir() and root == path.parent
                and old.parent == new.parent and old != new, "PATH_RELATIONSHIP_INVALID")
        require(not new.is_relative_to(repo) and not new.is_relative_to(root)
                and not root.is_relative_to(old), "PATH_RELATIONSHIP_INVALID")
        task = cls(path, approved, data, repo, root, old, new)
        task.validate()
        return task

    def context(self) -> None:
        data = self.data
        require(isinstance(data["native_thread_id"], str) and THREAD.fullmatch(data["native_thread_id"])
                is not None and data["native_thread_id"] == os.environ.get("CODEX_THREAD_ID"), "THREAD_DRIFT")
        fields(data["environment"], ("HOME", "CODEX_HOME"), "ENVIRONMENT_INVALID")
        require(isinstance(data["environment"]["HOME"], str) and Path(data["environment"]["HOME"]).is_absolute()
                and all(data["environment"][key] == os.environ.get(key) for key in ("HOME", "CODEX_HOME")),
                "NATIVE_ENVIRONMENT_DRIFT")
        require(type(data["expires_at"]) in (int, float) and math.isfinite(data["expires_at"])
                and data["expires_at"] > time.time(), "AUTHORIZATION_EXPIRED")

    def validate(self) -> None:
        self.context()
        data = self.data
        for value in (data["action_id"], data["candidate"]["build_id"]):
            require(isinstance(value, str) and ID.fullmatch(value) is not None, "RELEASE_ID_INVALID")
        require(isinstance(data["candidate"]["version"], str)
                and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.dev[0-9]+)?", data["candidate"]["version"])
                is not None, "VERSION_INVALID")
        require(type(data["expires_at"]) in (int, float), "EXPIRY_INVALID")
        modules, tests = data["modules"], data["tests"]
        require(isinstance(modules, list) and (0 if "runtime_build" in data or "agent_prompt_updates" in data else 1) <= len(modules) <= 64,
                "MODULES_INVALID")
        for name in modules:
            relative(name)
            require(Path(name).suffix in (".py", ".json"), "MODULES_INVALID", name)
            absolute(str(self.repo / "src" / PACKAGE / name))
        require(len(set(modules)) == len(modules), "MODULES_INVALID")
        require(isinstance(tests, list) and 1 <= len(tests) <= 64
                and all(isinstance(name, str) and re.fullmatch(r"test_[A-Za-z0-9_]+", name) for name in tests)
                and len(set(tests)) == len(tests), "TESTS_INVALID")
        expected = {f"src/{PACKAGE}/{name}" for name in modules} | {f"tests/{name}.py" for name in tests} | set(GUIDANCE)
        require(isinstance(data["postimages"], dict) and set(data["postimages"]) == expected,
                "POSTIMAGES_INCOMPLETE")
        fields(data["templates"], ("legacy", "release", "deploy", "installer"), "TEMPLATES_INVALID")
        paths = {key: file_identity(value) for key, value in data["templates"].items()}
        require(paths["legacy"] == LEGACY_PATH and data["templates"]["legacy"]["sha256"] == LEGACY_SHA256,
                "LEGACY_IDENTITY_INVALID")
        require(paths["release"].name == "release.py" and paths["deploy"] == paths["release"].with_name("deploy.py")
                and paths["installer"].name == "cutover_v22_once.py", "TEMPLATE_RELATIONSHIP_INVALID")
        require(file_identity(data["adapter"]) == self.repo / SCRIPT == Path(__file__).resolve(),
                "ADAPTER_IDENTITY_INVALID")
        self.validate_interpreters()
        fields(data["runtime_dependencies"], ("files", "trees"), "RUNTIME_DEPENDENCIES_INVALID")
        for key in ("files", "trees"):
            values = data["runtime_dependencies"][key]
            require(isinstance(values, list) and len(values) <= 32,
                    "RUNTIME_DEPENDENCIES_INVALID")
        fields(data["installed"]["metadata"], METADATA, "METADATA_INVALID")
        entry = data["installed"]["entry"]
        fields(entry, ("path", "realpath", "sha256"), "INSTALLED_ENTRY_INVALID")
        require(entry["path"] == str(Path(os.environ["HOME"]) / ".local/bin/nokiy-embedded-run")
                and entry["realpath"] == str(self.old / "caller/bin/nokiy-embedded-run"), "INSTALLED_ENTRY_INVALID")
        require(file_identity(data["authorization_ref"]) == self.root / "authorization.json",
                "AUTHORIZATION_REFERENCE_INVALID")
        require(document(self.root / "authorization.json").get("native_thread_id") == data["native_thread_id"],
                "AUTHORIZATION_THREAD_DRIFT")
        self.hashes()

    def validate_interpreters(self) -> None:
        fields(self.data["interpreters"], ("controller", "source", "candidate"), "INTERPRETERS_INVALID")
        source = self.repo / ".venv/bin/python"
        # An absent project environment may use only the already-pinned controller.
        # A broken or incomplete project environment must not silently fall back.
        if not os.path.lexists(self.repo / ".venv"):
            source = Path(sys.executable)
        required = {"controller": Path(sys.executable), "source": source,
                    "candidate": self.old / "caller/bin/python"}
        for key, path in required.items():
            require(file_identity(self.data["interpreters"][key]) == path,
                    "INTERPRETER_IDENTITY_INVALID", key)

    def runtime_build(self) -> dict[str, Any] | None:
        """Validate evidence, not a build command or an assertion of adapter execution."""
        if "runtime_build" not in self.data:
            return None
        pin = self.data["runtime_build"]
        fields(pin, ("path", "sha256"), "RUNTIME_BUILD_IDENTITY_INVALID")
        path = absolute(pin["path"])
        file_identity(pin)
        build = document(path, limit=EVIDENCE_LIMIT)
        fields(build, ("argv", "exit_code", "log", "sha256", "seconds", "source_hashes",
                       "external_model_calls", "binary_hashes") +
                      (("catalog_updates",) if "catalog_updates" in build else ()),
               "RUNTIME_BUILD_FIELDS_INVALID")
        require(type(build["exit_code"]) is int and build["exit_code"] == 0, "RUNTIME_BUILD_FAILED")
        require(type(build["external_model_calls"]) is int and build["external_model_calls"] == 0,
                "RUNTIME_BUILD_MODEL_CALLS")
        require(isinstance(build["argv"], list) and 1 <= len(build["argv"]) <= 128
                and all(isinstance(arg, str) and arg and "\0" not in arg for arg in build["argv"]),
                "RUNTIME_BUILD_ARGV_INVALID")
        require(type(build["seconds"]) in (int, float) and math.isfinite(build["seconds"])
                and build["seconds"] >= 0, "RUNTIME_BUILD_SECONDS_INVALID")
        require(isinstance(build["source_hashes"], dict) and bool(build["source_hashes"]),
                "RUNTIME_BUILD_SOURCES_INVALID")
        require(isinstance(build["binary_hashes"], dict) and set(build["binary_hashes"]) == set(RUNTIME_BINS),
                "RUNTIME_BUILD_BINS_INVALID")
        cache = Path(self.data["templates"]["installer"]["path"]).parent
        files = [(build["log"], build["sha256"]), *build["source_hashes"].items(),
                 *((str(cache / "target/release" / name), value)
                   for name, value in build["binary_hashes"].items())]
        for name, expected in files:
            source = absolute(name)
            regular(source)
            require(sha(source) == digest(expected), "INPUT_DRIFT", str(source))
        self.catalog_updates(build)
        return build

    def catalog_updates(self, build: dict[str, Any]) -> dict[str, Any]:
        updates = build.get("catalog_updates", {})
        require(isinstance(updates, dict) and len(updates) <= 32, "CATALOG_UPDATES_INVALID")
        old = document(self.old / "runtime-image.json")["artifacts"]
        destinations: set[Path] = set()
        for name, pin in updates.items():
            require(isinstance(name, str) and re.fullmatch(r"command_catalog_[0-9]+", name)
                    is not None and name in old, "CATALOG_SCOPE_INVALID", str(name))
            fields(pin, ("path", "sha256", "previous_sha256"), "CATALOG_IDENTITY_INVALID")
            source, prior = absolute(pin["path"]), absolute(old[name]["path"])
            regular(source)
            regular(prior)
            require(prior.is_relative_to(self.old / "runtime"), "CATALOG_SCOPE_INVALID", name)
            target = prior.relative_to(self.old / "runtime")
            require(re.fullmatch(r"(?:crates/tools/src/(?:command_run|commands/[A-Za-z0-9_]+)"
                                 r"|commands/[A-Za-z0-9_]+)/(?:schema\.json|prompt\.md)", str(target)) is not None
                    and source.parts[-len(target.parts):] == target.parts,
                    "CATALOG_SCOPE_INVALID", name)
            require(prior not in destinations, "CATALOG_DESTINATION_COLLISION", name)
            destinations.add(prior)
            require(digest(pin["previous_sha256"]) == old[name]["sha256"] == sha(prior),
                    "CATALOG_PREIMAGE_DRIFT", name)
            require(build["source_hashes"].get(str(source)) == digest(pin["sha256"])
                    and sha(source) == pin["sha256"], "CATALOG_SOURCE_UNBOUND", name)
            if target.name == "prompt.md":
                require(0 < source.stat().st_size <= LIMIT, "CATALOG_PROMPT_INVALID", name)
                try:
                    text = source.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    raise PreparationError("CATALOG_PROMPT_INVALID", name) from exc
                require(bool(text.strip()) and "\0" not in text, "CATALOG_PROMPT_INVALID", name)
            else:
                before, after = document(prior), document(source)
                require(before.get("name") == after.get("name")
                        and isinstance(after.get("input_schema", after), dict)
                        and after.get("input_schema", after).get("type") == "object",
                        "CATALOG_SCHEMA_INVALID", name)
        return updates

    def prompt_updates(self) -> dict[str, Any]:
        if "agent_prompt_updates" not in self.data:
            return {}
        require("runtime_build" not in self.data, "PROMPT_BUILD_MIXED_SCOPE")
        return validate_agent_prompt_updates(self.old, self.data["agent_prompt_updates"])

    def artifact_updates(self, build: dict[str, Any] | None) -> dict[str, Any]:
        return self.catalog_updates(build) if build is not None else self.prompt_updates()

    def hashes(self) -> dict[str, str]:
        """ONE complete input/recipe set, used by execution, reuse and every phase."""
        self.context()
        self.validate_interpreters()
        values: dict[str, str] = {}

        def add(path: Path, expected: str, *, tree: bool = False) -> None:
            expected = digest(expected)
            require(str(path) not in values or values[str(path)] == expected, "INPUT_COLLISION", str(path))
            observed = tree_sha256(path) if tree else sha(path)
            require(observed == expected, "INPUT_DRIFT", str(path))
            values[str(path)] = observed

        add(self.path, self.manifest_sha256)
        for relative_path, expected in self.data["postimages"].items():
            path = absolute(str(self.repo / relative(relative_path)))
            regular(path)
            add(path, expected)
        pins = [self.data["adapter"], self.data["authorization_ref"],
                *self.data["templates"].values(), *self.data["interpreters"].values(),
                *self.data["runtime_dependencies"]["files"]]
        config_path = Path(os.environ.get("CODEX_HOME") or str(Path(os.environ["HOME"]) / ".codex")) / "config.toml"
        if self.data["config"] is None:
            require(not os.path.lexists(config_path), "CONFIG_DRIFT")
        else:
            require(self.data["config"].get("path") == str(config_path), "CONFIG_PATH_INVALID")
            pins.append(self.data["config"])
        for pin in pins:
            path = file_identity(pin)
            add(path, pin["sha256"])
            if path.resolve(strict=True) != path:
                add(path.resolve(strict=True), pin["sha256"])
        build = self.runtime_build()
        for pin in self.prompt_updates().values():
            add(absolute(pin["path"]), pin["sha256"])
        if build is not None:
            pin = self.data["runtime_build"]
            add(absolute(pin["path"]), pin["sha256"])
            add(absolute(build["log"]), build["sha256"])
            for path, expected in build["source_hashes"].items():
                add(absolute(path), expected)
            cache = Path(self.data["templates"]["installer"]["path"]).parent
            for name, expected in build["binary_hashes"].items():
                add(absolute(str(cache / "target/release" / name)), expected)
        for name, expected in self.data["installed"]["metadata"].items():
            path = absolute(str(self.old / name))
            regular(path)
            add(path, expected)
        add(absolute(self.data["installed"]["entry"]["realpath"]), self.data["installed"]["entry"]["sha256"])
        trees = [*self.data["runtime_dependencies"]["trees"],
                 {"path": str(self.old / SITE / PACKAGE), "sha256": self.data["installed"]["package_sha256"]}]
        for pin in trees:
            fields(pin, ("path", "sha256"), "TREE_IDENTITY_INVALID")
            path = absolute(pin["path"])
            require(not self.new.is_relative_to(path) and not self.root.is_relative_to(path), "TREE_SCOPE_INVALID")
            add(path, pin["sha256"], tree=True)
        artifacts = document(self.old / "runtime-image.json").get("artifacts")
        require(isinstance(artifacts, dict) and {"tura_router", "tura_session_db"} <= set(artifacts),
                "RUNTIME_IMAGE_INVALID")
        for artifact in artifacts.values():
            require(isinstance(artifact, dict) and {"path", "sha256"} <= set(artifact), "RUNTIME_IMAGE_INVALID")
            path = absolute(artifact["path"])
            regular(path)
            add(path, artifact["sha256"])
        # Test imports can reach helpers not named as unittest entry modules.
        # Fingerprint the complete source/test trees in this SAME function, so
        # the executed receipt and later reuse comparator cannot diverge.
        for root in (self.repo / "src" / PACKAGE, self.repo / "tests"):
            observed = tree_sha256(root)
            require(str(root) not in values or values[str(root)] == observed, "INPUT_COLLISION", str(root))
            values[str(root)] = observed
        for root in (Path(self.data["interpreters"]["source"]["path"]).parent.parent, self.old / "caller"):
            path = root / "pyvenv.cfg"
            if os.path.lexists(path):
                regular(absolute(str(path)))
                observed = sha(path)
                require(str(path) not in values or values[str(path)] == observed, "INPUT_COLLISION", str(path))
                values[str(path)] = observed
        return dict(sorted(values.items()))

    def installed(self, *, live: bool = False) -> None:
        entry = Path(self.data["installed"]["entry"]["path"])
        target = (self.new if live else self.old) / "caller/bin/nokiy-embedded-run"
        require(entry.resolve(strict=True) == target, "INSTALLED_PREIMAGE_DRIFT", str(entry))
        regular(target)
        expected = (document(self.out / "pins.json", limit=EVIDENCE_LIMIT)[str(target)]
                    if live else self.data["installed"]["entry"]["sha256"])
        require(sha(entry) == expected, "INSTALLED_PREIMAGE_DRIFT", str(entry))

    def argv(self, phase: str) -> list[str]:
        return [self.data["interpreters"]["controller"]["path"], "-B", str(self.repo / SCRIPT),
                "--manifest", str(self.path), "--manifest-sha256", self.manifest_sha256, "--phase", phase]


def _import_legacy(path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("nokiy_caller_delivery_template", path)
    require(spec is not None and spec.loader is not None, "TEMPLATE_IMPORT_INVALID")
    module = importlib.util.module_from_spec(spec)
    unit = ast.parse(path.read_bytes(), filename=str(path))
    functions = [node for node in unit.body if isinstance(node, ast.FunctionDef) and node.name == "tests"]
    require(len(functions) == 1 and not functions[0].decorator_list, "LEGACY_TEST_RUNNER_API_DRIFT")
    pattern = ast.parse('interpreter = REPO / ".venv/bin/python" if label == "source-tests" '
                        'else OLD / "caller/bin/python"').body[0]
    matches = [node for node in ast.walk(functions[0])
               if isinstance(node, ast.Assign) and ast.dump(node) == ast.dump(pattern)]
    require(len(matches) == 1, "LEGACY_TEST_RUNNER_API_DRIFT")
    # Keep the pinned runner and candidate branch; bind only its source interpreter.
    matches[0].value.body = ast.Name(id="NOKIY_SOURCE_INTERPRETER", ctx=ast.Load())
    exec(compile(ast.fix_missing_locations(unit), str(path), "exec"), module.__dict__)
    return module


@contextmanager
def handoff_environment(task: Task, *, runtime: bool = False):
    path = (task.new if runtime else task.old) / "runtime-image.json"
    image = document(path)
    values = {"NOKIY_RUNTIME_EVIDENCE_HANDOFF_IMAGE": str(path),
              "NOKIY_VERIFIER_TEST_ROUTER": image["artifacts"]["tura_router"]["path"],
              "NOKIY_VERIFIER_TEST_SESSION_DB": image["artifacts"]["tura_session_db"]["path"]}
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Adapter:
    def __init__(self, task: Task):
        self.task = task
        task.hashes()  # Before executing any template code.
        legacy = self.legacy = _import_legacy(Path(task.data["templates"]["legacy"]["path"]))
        self.executed_tests = legacy.tests
        self.runtime_tests_executed: set[str] = set()
        self.test_evidence: dict[str, dict[str, Any]] = {}
        self.catalog_staged = False
        bindings = dict(ROOT=task.root, HERE=task.here, REPO=task.repo, OLD=task.old, NEW=task.new,
                        SITE=SITE, PACKAGE=PACKAGE, CACHE=Path(task.data["templates"]["installer"]["path"]).parent,
                        RECIPE=Path(task.data["templates"]["release"]["path"]).parent,
                        NOKIY_SOURCE_INTERPRETER=Path(task.data["interpreters"]["source"]["path"]),
                        THREAD=task.data["native_thread_id"], ACTION=task.data["action_id"],
                        MODULES=list(task.data["modules"]), TESTS=list(task.data["tests"]), hashes=task.hashes, tests=self.tests)
        for key, value in bindings.items():
            setattr(legacy, key, value)
        original_load, captured = legacy.load, {}

        def pinned_load(name: str, path: Path):
            key = {"release": "release", "concurrency_deploy": "deploy"}.get(name)
            require(key is not None and str(path) == task.data["templates"][key]["path"], "TEMPLATE_IMPORT_INVALID")
            task.hashes()
            module = original_load(name, path)
            if key == "release":
                captured["save"] = module.save
            task.hashes()
            return module

        legacy.load = pinned_load
        self.package, self.deploy = legacy.recipes()
        # The historical helper wraps save to force Sol. Restore the *original*
        # audited release API captured at import; never infer a new default model.
        require("save" in captured, "TEMPLATE_API_INVALID")
        self.package.save = captured["save"]
        self.package.VERSION = task.data["candidate"]["version"]
        self.package.BUILD_ID = self.deploy.BUILD_ID = task.data["candidate"]["build_id"]
        require(list(self.package.MODULES) == task.data["modules"] and list(legacy.TESTS) == task.data["tests"]
                and not self.package.RUNTIME_BINS and not self.package.RUST_CHECKS, "CALLER_SCOPE_DRIFT")
        self.package.RUNTIME_BINS = self.deploy.RUNTIME_BINS = RUNTIME_BINS if "runtime_build" in task.data else ()
        if "runtime_build" in task.data:
            self.package.CACHE = legacy.CACHE
            build = task.runtime_build()
            if task.catalog_updates(build):
                original_save = self.package.save
                self.package.save = lambda path, value: self.stage_save(original_save, path, value)
                self.catalog_validator(build)
        elif task.prompt_updates():
            original_save = self.package.save
            self.package.save = lambda path, value: self.stage_save(original_save, path, value)
            self.catalog_validator(None)
        require(Path(self.deploy.installer.__file__).resolve() == Path(task.data["templates"]["installer"]["path"]),
                "INSTALLER_IDENTITY_INVALID")
        self.lock_identity()
        task.hashes()

    def catalog_validator(self, build: dict[str, Any] | None) -> None:
        """Adapt one obsolete equality in the pinned validator; retain its other checks."""
        path = Path(self.task.data["templates"]["deploy"]["path"])
        original = self.deploy.validate
        require(getattr(getattr(original, "__code__", None), "co_filename", None) == str(path),
                "CATALOG_VALIDATOR_API_DRIFT")
        functions = [node for node in ast.parse(path.read_bytes()).body
                     if isinstance(node, ast.FunctionDef) and node.name == "validate"]
        require(len(functions) == 1 and not functions[0].decorator_list, "CATALOG_VALIDATOR_API_DRIFT")
        pattern = ast.parse('expected = build["binary_hashes"][name] if name in RUNTIME_BINS '
                            'else old_image["artifacts"][name]["sha256"]').body[0]
        matches = [node for node in ast.walk(functions[0])
                   if isinstance(node, ast.Assign) and ast.dump(node) == ast.dump(pattern)]
        require(len(matches) == 1, "CATALOG_VALIDATOR_API_DRIFT")
        hashes = {name: pin["sha256"] for name, pin in self.task.artifact_updates(build).items()}
        namespace = dict(original.__globals__)
        require("NOKIY_DECLARED_CATALOG_HASHES" not in namespace, "CATALOG_VALIDATOR_API_DRIFT")
        namespace["NOKIY_DECLARED_CATALOG_HASHES"] = hashes
        matches[0].value = ast.parse(
            'build["binary_hashes"][name] if name in RUNTIME_BINS else '
            'NOKIY_DECLARED_CATALOG_HASHES[name] if name in NOKIY_DECLARED_CATALOG_HASHES '
            'else old_image["artifacts"][name]["sha256"]', mode="eval").body
        unit = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
        exec(compile(unit, str(path), "exec"), namespace)
        self.deploy.validate = namespace["validate"]

    def stage_save(self, original_save: Any, path: Path, value: Any) -> None:
        task = self.task
        staging = task.new.with_name("." + task.new.name + ".staging")
        if Path(path) == staging / "runtime-image.json":
            build = task.runtime_build()
            updates = task.artifact_updates(build)
            old = document(task.old / "runtime-image.json")["artifacts"]
            require(not self.catalog_staged and not task.new.exists()
                    and staging.resolve(strict=True) == staging,
                    "CATALOG_STAGE_INVALID")
            for name, pin in updates.items():
                prior = Path(old[name]["path"])
                relative_path = prior.relative_to(task.old)
                target = absolute(str(staging / relative_path))
                regular(target)
                require(value["artifacts"][name]["path"] == str(task.new / relative_path)
                        and value["artifacts"][name]["sha256"] == sha(target) == pin["previous_sha256"],
                        "CATALOG_STAGE_DRIFT", name)
                # A staged catalog may share the old inode. Never overwrite it.
                target.unlink()
                with Path(pin["path"]).open("rb") as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                require(sha(target) == pin["sha256"] and sha(prior) == pin["previous_sha256"],
                        "CATALOG_STAGE_DRIFT", name)
                value["artifacts"][name].update(sha256=pin["sha256"], size=target.stat().st_size)
            task.hashes()
            self.catalog_staged = True
        elif Path(path) == staging / "release-provenance.json":
            updates = task.artifact_updates(task.runtime_build())
            require(value.get("changed_runtime_artifacts") == list(self.package.RUNTIME_BINS), "CATALOG_STAGE_DRIFT")
            value["changed_runtime_artifacts"] += sorted(updates)
            value["catalog_updates"] = updates
        original_save(path, value)

    def check_pins(self) -> None:
        # Only Task.hashes() may admit a non-canonical, exact-realpath-bound input.
        inputs = self.task.hashes()
        for path, expected_sha in document(self.task.out / "pins.json", limit=EVIDENCE_LIMIT).items():
            observed = inputs[path] if path in inputs else sha(absolute(path))
            require(observed == expected_sha, "PIN_DRIFT", path)
        if "runtime_build" in self.task.data or "agent_prompt_updates" in self.task.data:
            self.runtime_artifacts()

    def runtime_artifacts(self) -> dict[str, Any]:
        task = self.task
        build = task.runtime_build()
        require(build is not None or task.prompt_updates(), "RUNTIME_BUILD_REQUIRED")
        updates = task.artifact_updates(build)
        old = document(task.old / "runtime-image.json")["artifacts"]
        new = document(task.new / "runtime-image.json").get("artifacts")
        require(isinstance(new, dict) and set(new) == set(old) | (set(RUNTIME_BINS) if build else set()), "RUNTIME_DRIFT")
        for name, value in new.items():
            require(isinstance(value, dict) and {"path", "sha256"} <= set(value), "RUNTIME_DRIFT", name)
            path = absolute(value["path"])
            regular(path)
            expected = (build["binary_hashes"][name] if build is not None and name in RUNTIME_BINS else
                        updates[name]["sha256"] if name in updates else old[name]["sha256"])
            require(value["sha256"] == expected == sha(path)
                    and (name not in set(RUNTIME_BINS) | set(updates) or path.is_relative_to(task.new)),
                    "RUNTIME_DRIFT", name)
            if name in updates:
                require(path == task.new / Path(old[name]["path"]).relative_to(task.old)
                        and value.get("size") == path.stat().st_size, "RUNTIME_DRIFT", name)
        return new

    def lock_identity(self) -> dict[str, int]:
        path = absolute(str(self.deploy.installer.LOCK))
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600, "INSTALLER_LOCK_INVALID")
        return {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode)}

    def test_binding(self, package_root: Path, *, source: bool = False, runtime: bool = False) -> dict[str, Any]:
        task = self.task
        env = {key: value for key, value in os.environ.items()
               if key not in ("NOKIY_DEPLOYMENT_PLAN_SHA256", "NOKIY_DEPLOYMENT_ACTION_ID")}
        image_path = (task.new if runtime else task.old) / "runtime-image.json"
        image = document(image_path)
        artifacts = self.runtime_artifacts() if runtime else image["artifacts"]
        env.update(PYTHONPATH="<byte-identical-package>" + os.pathsep + str(task.repo / "tests"),
                   PYTHONDONTWRITEBYTECODE="1", NOKIY_RUNTIME_EVIDENCE_HANDOFF_IMAGE=str(image_path),
                   NOKIY_VERIFIER_TEST_ROUTER=artifacts["tura_router"]["path"],
                   NOKIY_VERIFIER_TEST_SESSION_DB=artifacts["tura_session_db"]["path"])
        binding = {"input_hashes": task.hashes(), "package": package_bytes(package_root / PACKAGE),
                "interpreter": task.data["interpreters"]["source" if source else "candidate"],
                "test_modules": task.data["tests"], "environment_sha256": canonical(env)}
        if runtime:
            binding.update(runtime_acceptance=True, runtime_image=identity(image_path),
                           runtime_artifacts={name: identity(Path(value["path"])) for name, value in artifacts.items()})
        return binding

    def tests(self, label: str, package_root: Path, test_root: Path | None = None) -> None:
        task = self.task
        require(label in ("source-tests", "candidate-tests", "staged-tests", "live-tests"), "TEST_LABEL_INVALID")
        # Legacy stage and live verification pass this unused marker, not an alternative test tree.
        if (label in ("staged-tests", "live-tests") and test_root is not None
                and Path(test_root) == task.here / "validation-tests"):
            test_root = task.repo / "tests"
        require(test_root is None or Path(test_root).resolve() == task.repo / "tests", "TEST_ROOT_INVALID")
        root = Path(package_root)
        expected = {"source-tests": task.repo / "src", "candidate-tests": task.here / "validation-package",
                    "staged-tests": task.new / SITE, "live-tests": task.new / SITE}[label]
        require(root.resolve(strict=True) == expected, "TEST_PACKAGE_ROOT_INVALID")
        runtime = "runtime_build" in task.data and label in ("staged-tests", "live-tests")
        before = self.test_binding(root, source=label == "source-tests", runtime=runtime)
        receipt_path = task.here / (label + ".json")
        if runtime:
            baseline = document(task.here / "candidate-tests-binding.json", limit=EVIDENCE_LIMIT)
            require(all(baseline[key] == before[key] for key in ("input_hashes", "package", "interpreter", "test_modules")),
                    "TEST_INPUT_DRIFT")
        execute = label in ("source-tests", "candidate-tests") or runtime
        refresh = None
        if not execute:
            # Validate candidate evidence before deciding between reuse and a fresh run.
            prior_path = task.here / "candidate-tests.json"
            prior_ref = identity(prior_path)
            prior = document(prior_path, limit=EVIDENCE_LIMIT)
            require(type(prior.get("exit_code")) is int and prior["exit_code"] == 0
                    and prior.get("execution_reused", False) is False
                    and prior.get("source_hashes") == task.hashes()
                    and prior.get("log") == str(task.here / "candidate-tests.log")
                    and prior.get("interpreter") == str(Path(before["interpreter"]["path"]).resolve())
                    and sha(Path(prior["log"])) == prior.get("sha256"), "REUSE_RECEIPT_INVALID")
            binding_path = task.here / "candidate-tests-binding.json"
            binding_ref = identity(binding_path)
            baseline = document(binding_path, limit=EVIDENCE_LIMIT)
            require(set(baseline) == set(before)
                    and all(baseline[key] == before[key] for key in before if key != "environment_sha256"),
                    "REUSE_BINDING_DRIFT")
            previous_environment = digest(baseline["environment_sha256"])
            environment = digest(before["environment_sha256"])
            if previous_environment != environment:
                refresh = {"cause": "environment_sha256_changed", "candidate_receipt": prior_ref,
                           "candidate_binding": binding_ref, "previous_environment_sha256": previous_environment,
                           "environment_sha256": environment}
        if execute or refresh is not None:
            prior_old = self.legacy.OLD
            try:
                # The pinned helper derives its router paths from OLD, overriding env.
                if runtime:
                    self.legacy.OLD = task.new
                with handoff_environment(task, runtime=runtime):
                    self.executed_tests(label, root, test_root)
            except Exception as error:
                raise PreparationError("TEST_EXECUTION_FAILED", label + ": " + type(error).__name__) from error
            finally:
                self.legacy.OLD = prior_old
            receipt_ref = identity(receipt_path)
            receipt = document(receipt_path, limit=EVIDENCE_LIMIT)
            require(type(receipt.get("exit_code")) is int and receipt["exit_code"] == 0
                    and receipt.get("execution_reused", False) is False
                    and receipt.get("source_hashes") == before["input_hashes"]
                    and receipt.get("log") == str(task.here / (label + ".log"))
                    and receipt.get("interpreter") == str(Path(before["interpreter"]["path"]).resolve())
                    and sha(Path(receipt["log"])) == receipt.get("sha256"), "TEST_RECEIPT_INVALID")
            require(self.test_binding(root, source=label == "source-tests", runtime=runtime) == before, "TEST_INPUT_DRIFT")
            binding_path = task.here / (label + "-binding.json")
            save(binding_path, before)
            binding_ref = identity(binding_path)
            if runtime:
                self.runtime_tests_executed.add(label)
        else:
            receipt = {**prior, "execution_reused": True, "reused_from": prior_ref,
                       "verified_package_root": str(root), "reuse_basis": "Exact declared inputs and automatically recorded bindings unchanged; not a hermetic host OS or standard library attestation"}
            save(receipt_path, receipt)
            receipt_ref = identity(receipt_path)
        if refresh is not None:
            # Create an audit projection; never rewrite candidate or executor artifacts.
            receipt = {**receipt, "execution_reused": False, "execution_receipt": receipt_ref,
                       "execution_refresh": refresh}
            refresh_path = task.here / (label + "-refresh.json")
            save(refresh_path, receipt)
            receipt_ref = identity(refresh_path)
        # Project only checks completed above; full inputs and logs stay in artifacts.
        image_path = (task.new if runtime else task.old) / "runtime-image.json"
        self.test_evidence[label] = {
            "receipt": receipt_ref, "log": {"path": receipt["log"], "sha256": receipt["sha256"]},
            "binding": binding_ref, "package_root": str(root), "exit_code": receipt["exit_code"],
            "execution_reused": receipt.get("execution_reused", False),
            "runtime_image": (before["runtime_image"] if runtime else
                              {"path": str(image_path), "sha256": before["input_hashes"][str(image_path)]})}
        if self.test_evidence[label]["execution_reused"]:
            self.test_evidence[label]["reused_from"] = receipt["reused_from"]
        elif refresh is not None:
            self.test_evidence[label]["execution_refresh"] = refresh
            self.test_evidence[label]["execution_receipt"] = receipt["execution_receipt"]

    def paired_wire_binding(self) -> dict[str, Any]:
        task = self.task
        image = task.new / "runtime-image.json"
        artifacts = (self.runtime_artifacts() if "runtime_build" in task.data or "agent_prompt_updates" in task.data else
                     document(image)["artifacts"])
        return {"manifest_sha256": task.manifest_sha256, "input_hashes": task.hashes(),
                "candidate": task.data["candidate"], "package_root": str(task.new / SITE),
                "package": package_bytes(task.new / SITE / PACKAGE),
                "interpreter": task.data["interpreters"]["candidate"],
                "runtime_image": identity(image),
                "runtime_artifacts": {name: identity(Path(row["path"])) for name, row in artifacts.items()},
                "native_cli": identity(NATIVE_CLI),
                "fixture": identity(task.repo / "tests/test_verifier_parent.py"), "test": PAIRED_WIRE_TEST}

    def execute_paired_wire(self, binding_path: Path, receipt_path: Path, log: Path) -> int:
        """A fixed native test, never the manifest's test selection or an inference CLI."""
        task = self.task
        bound = document(binding_path, limit=EVIDENCE_LIMIT)
        require(bound == self.paired_wire_binding(), "PAIRED_WIRE_BINDING_DRIFT")
        scratch = task.here / "paired-wire-scratch"
        scratch.mkdir(mode=0o700)
        env = {key: os.environ[key] for key in ("HOME", "CODEX_HOME") if key in os.environ}
        env.update({"PATH": "/usr/bin:/bin",
               "LANG": "C.UTF-8", "TMPDIR": str(scratch), "NOKIY_TEST_ROOT": str(scratch),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
               "PYTHONPATH": str(task.new / SITE) + os.pathsep + str(task.repo / "tests"),
               "NOKIY_VERIFIER_TEST_ROUTER": bound["runtime_artifacts"]["tura_router"]["path"],
               "NOKIY_VERIFIER_TEST_SESSION_DB": bound["runtime_artifacts"]["tura_session_db"]["path"],
               "NOKIY_NATIVE_SANDBOX_TEST_BIN": bound["native_cli"]["path"],
               "NOKIY_PAIRED_WIRE_BINDING": str(binding_path),
               "NOKIY_PAIRED_WIRE_RECEIPT": str(receipt_path)})
        with log.open("xb") as output:
            # Trusted, SHA-bound orchestration needs the native parent executor:
            # each service and the ParentVerifier workload applies its own fence once.
            result = subprocess.run([bound["interpreter"]["path"],
                "-B", "-m", "unittest", PAIRED_WIRE_TEST, "-v"],
                cwd=task.repo, env=env, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, timeout=90)
        return result.returncode

    def check_paired_wire(self, evidence: dict[str, Any]) -> None:
        task = self.task
        for key, leaf in (("binding", "paired-wire-binding.json"), ("receipt", "paired-wire.json"),
                          ("log", "paired-wire.log")):
            require(evidence[key]["path"] == str(task.here / leaf), "PAIRED_WIRE_EVIDENCE_INVALID")
            file_identity(evidence[key])
        require(document(Path(evidence["binding"]["path"]), limit=EVIDENCE_LIMIT) == self.paired_wire_binding(),
                "PAIRED_WIRE_BINDING_DRIFT")
        receipt = document(Path(evidence["receipt"]["path"]), limit=EVIDENCE_LIMIT)
        require(receipt.get("binding") == evidence["binding"] and receipt.get("test") == PAIRED_WIRE_TEST
                and receipt.get("passed") is True and receipt.get("execution_reused") is False,
                "PAIRED_WIRE_RECEIPT_INVALID")
        result = receipt["result"]
        require(result.get("success") is True and type(result.get("exit_code")) is int
                and result["exit_code"] == 0 and result.get("outcome") == "known"
                and result.get("process_reaped") is True and result.get("process_group_empty") is True,
                "PAIRED_WIRE_RESULT_INVALID")
        refs = []
        for name in ("stdout", "stderr"):
            row = result["verifier_output"]["streams"][name]
            path = absolute(row["path"])
            require(path.parent == task.here / "paired-wire-output" and row.get("complete") is True
                    and row.get("durable") is True and path.stat().st_size == row["bytes"],
                    "PAIRED_WIRE_OUTPUT_INVALID")
            ref = {"path": str(path), "sha256": row["sha256"]}
            file_identity(ref)
            refs.append(ref)
        require(evidence["outputs"] == refs, "PAIRED_WIRE_OUTPUT_INVALID")

    def paired_wire(self) -> dict[str, Any]:
        task = self.task
        binding_path, receipt_path, log = (task.here / name for name in
            ("paired-wire-binding.json", "paired-wire.json", "paired-wire.log"))
        require(not os.path.lexists(receipt_path) and not os.path.lexists(task.here / "paired-wire-output"),
                "PAIRED_WIRE_EVIDENCE_EXISTS")
        save(binding_path, self.paired_wire_binding())
        require(self.execute_paired_wire(binding_path, receipt_path, log) == 0, "PAIRED_WIRE_FAILED")
        # unittest exit 0 on a skip is insufficient: only the completed roundtrip writes this receipt.
        require(receipt_path.is_file(), "PAIRED_WIRE_EXECUTION_REQUIRED")
        result = document(receipt_path, limit=EVIDENCE_LIMIT)["result"]
        evidence = {"binding": identity(binding_path), "receipt": identity(receipt_path), "log": identity(log),
                    "outputs": [{"path": row["path"], "sha256": row["sha256"]} for row in
                                (result["verifier_output"]["streams"][name] for name in ("stdout", "stderr"))]}
        self.check_paired_wire(evidence)
        return evidence

    def prepare(self) -> dict[str, Any]:
        task = self.task
        task.installed()
        require(not os.path.lexists(task.here) and not os.path.lexists(task.new), "PARTIAL_PREPARATION_EXISTS")
        for name in ("index.lock", "HEAD.lock", "config.lock"):
            require(not os.path.lexists(task.repo / ".git" / name), "REPO_LOCK_PRESENT", name)
        task.here.mkdir()
        save(task.here / "preimages.json", {"entry": task.data["installed"]["entry"], "postimages": task.hashes()})
        self.tests("source-tests", task.repo / "src")
        candidate = task.here / "validation-package"
        shutil.copytree(task.old / SITE / PACKAGE, candidate / PACKAGE, ignore=shutil.ignore_patterns("__pycache__"))
        (candidate / PACKAGE).chmod(0o755)
        for name in task.data["modules"]:
            target = candidate / PACKAGE / name
            # Only this disposable validation clone is made writable, never installed files.
            parent = candidate / PACKAGE
            for part in Path(name).parts[:-1]:
                parent = parent / part
                if not parent.exists():
                    parent.mkdir()
                parent.chmod(0o755)
            if target.exists():
                target.chmod(0o644)
            shutil.copy2(task.repo / "src" / PACKAGE / name, target)
        self.tests("candidate-tests", candidate)
        # The immutable packager requires file digests; other evidence retains complete hashes.
        file_hashes = {path: value for path, value in task.hashes().items() if Path(path).is_file()}
        build = task.runtime_build()
        save(task.here / "adoption.json", {"postimages": file_hashes, "provider_calls": 0,
             "scope": ("Exact listed caller modules and SHA-bound four-bin runtime; permissions and model/config unchanged"
                       if build is not None else
                       "Exact listed caller modules and agent prompts; runtime binaries and model/config unchanged"
                       if task.prompt_updates() else
                       "Exact listed caller modules; runtime, permissions and model/config unchanged"),
             "unchanged_runtime": build is None and not task.prompt_updates(),
             "unchanged_runtime_binaries": build is None,
             "agent_prompt_updates": task.prompt_updates()})
        if build is None:
            image = document(task.old / "runtime-image.json")
            observations = {name: sha(Path(value["path"])) for name, value in image["artifacts"].items()}
            save(task.here / "runtime-unchanged.json", observations)
            save(task.here / "rust-build.json", {"exit_code": 0, "executed_build": False, "binary_hashes": {},
                 "log": str(task.here / "runtime-unchanged.json"), "sha256": sha(task.here / "runtime-unchanged.json"),
                 "source_hashes": file_hashes})
        else:
            save(task.here / "rust-build.json", {**build, "source_hashes": file_hashes,
                 "build_receipt": task.data["runtime_build"], "receipt_source_hashes": build["source_hashes"],
                 "adapter_executed_build": False,
                 "source_hashes_scope": "Packaging projection equal to adoption.postimages, not executed build dependencies; original parent receipt is SHA-bound and unmodified"})
        task.installed()
        self.package.stage()  # Audited immutable packager, not a replacement implementation.
        if task.artifact_updates(build):
            require(self.catalog_staged, "CATALOG_STAGE_INVALID")
        require(package_bytes(task.new / SITE / PACKAGE) == package_bytes(candidate / PACKAGE), "STAGED_PACKAGE_DRIFT")
        old_binding, new_binding = (document(root / "caller-binding.json") for root in (task.old, task.new))
        require("default_profile" in old_binding and "default_profile" in new_binding
                and canonical(new_binding["default_profile"]) == canonical(old_binding["default_profile"]),
                "MODEL_PROFILE_DRIFT")
        new_image_ref = identity(task.new / "runtime-image.json")
        new_image = document(task.new / "runtime-image.json")
        if build is None and not task.prompt_updates():
            require(set(new_image["artifacts"]) == set(observations)
                    and all(value["sha256"] == observations[name] == sha(Path(value["path"]))
                            for name, value in new_image["artifacts"].items()), "RUNTIME_DRIFT")
        elif build is not None:
            self.runtime_artifacts()
            require("staged-tests" in self.runtime_tests_executed, "RUNTIME_TEST_EXECUTION_REQUIRED")
            require(document(task.here / "staged-tests-binding.json", limit=EVIDENCE_LIMIT) ==
                    self.test_binding(task.new / SITE, runtime=True), "TEST_INPUT_DRIFT")
        else:
            self.runtime_artifacts()
        labels = ("source-tests", "candidate-tests", "staged-tests")
        require(all(label in self.test_evidence for label in labels), "PREPARATION_TEST_EVIDENCE_REQUIRED")
        paired_wire = self.paired_wire()  # Mandatory for caller-only and paired runtime candidates alike.
        task.hashes()
        task.installed()
        task.out.mkdir()
        (task.out / "artifacts").mkdir()
        # Forward the parent's actual authorization verbatim. No invented standing approval/lease.
        with (task.out / "authorization.json").open("xb") as stream:
            stream.write((task.root / "authorization.json").read_bytes())
        save(task.out / "lock-identity.json", self.lock_identity())
        save(task.here / "preparation.json", {"schema_version": "nokiy_caller_preparation_v1",
             "manifest_sha256": task.manifest_sha256, "input_hashes": task.hashes(),
             "package": package_bytes(task.new / SITE / PACKAGE), "deployment_executed": False,
             "paired_wire": paired_wire})
        preparation_ref = identity(task.here / "preparation.json")
        inputs = task.hashes()
        save(task.out / "input-trees.json", {path: value for path, value in inputs.items() if Path(path).is_dir()})
        pins = {path: value for path, value in inputs.items() if Path(path).is_file()}
        evidence = [*task.here.glob("*.json"), *task.here.glob("*.log"), task.out / "authorization.json",
                    task.out / "lock-identity.json", task.out / "input-trees.json"]
        evidence += [root / name for root in (task.old, task.new) for name in METADATA]
        evidence += [task.new / "caller/bin/nokiy-embedded-run"]
        if build is not None:
            evidence += [absolute(value["path"]) for value in new_image["artifacts"].values()]
        pins.update({str(path): sha(path) for path in evidence})
        pins.update({ref["path"]: ref["sha256"] for ref in paired_wire["outputs"]})
        native_cli = self.paired_wire_binding()["native_cli"]
        pins[native_cli["path"]] = native_cli["sha256"]
        save(task.out / "pins.json", pins)
        self.deploy.validate()  # Existing offline validation; NEVER deploy.main/apply here.
        task.hashes()
        if build is not None:
            self.runtime_artifacts()
        task.installed()
        self.check_paired_wire(paired_wire)
        manifest_ref = {"path": str(task.path), "sha256": task.manifest_sha256}
        references = [manifest_ref, preparation_ref, new_image_ref,
                      *(paired_wire[key] for key in ("receipt", "binding", "log")), *paired_wire["outputs"]]
        for label in labels:
            result = self.test_evidence[label]
            references.extend(result[key] for key in ("receipt", "log", "binding", "runtime_image"))
            if result["execution_reused"]:
                references.append(result["reused_from"])
        checked = {}
        for ref in references:
            require(pins.get(ref["path"]) == ref["sha256"], "PREPARATION_EVIDENCE_DRIFT", ref["path"])
            checked[ref["path"]] = ref
        for ref in checked.values():
            file_identity(ref)  # Detect post-check/post-pin drift without executing tests again.
        files = [task.data["interpreters"]["controller"], task.data["adapter"], manifest_ref,
                 *task.data["templates"].values(), identity(task.out / "pins.json"),
                 identity(task.out / "authorization.json"), identity(task.out / "lock-identity.json"),
                 identity(task.out / "input-trees.json")]
        plan = {"schema_version": "nokiy_deployment_plan_v1", "action_id": task.data["action_id"],
                "native_thread_id": task.data["native_thread_id"], "workspace": str(task.root),
                "artifact_root": str(task.out / "artifacts"), "target": "nokiy-embedded-run", "release": task.new.name,
                "expires_at": task.data["expires_at"], "authorization_ref": identity(task.out / "authorization.json"),
                "commands": {phase: {"argv": task.argv(phase), "files": files, "timeout_seconds": 180} for phase in PHASES}}
        save(task.out / "plan.json", plan)
        return {"plan": str(task.out / "plan.json"), "sha256": canonical(plan),
                "preparation_evidence": {
                    "status": "PREPARED_NOT_DEPLOYED", "deployment_executed": False, "parent_acceptance": False,
                    "native_thread_id": task.data["native_thread_id"], "action_id": task.data["action_id"],
                    "manifest": manifest_ref, "preparation": preparation_ref, "plan": identity(task.out / "plan.json"),
                    "candidate": {**task.data["candidate"], "runtime_image": new_image_ref},
                    "runtime_mode": "runtime-build" if build is not None else
                                    "agent-prompt-only" if task.prompt_updates() else "caller-only",
                    "runtime_build": task.data.get("runtime_build"), "adapter_executed_build": False,
                    "paired_wire": paired_wire,
                    "tests": {label: self.test_evidence[label] for label in labels}}}

    def phase(self, phase: str) -> None:
        task = self.task
        plan = document(task.out / "plan.json")
        require(canonical(plan) == os.environ.get("NOKIY_DEPLOYMENT_PLAN_SHA256")
                and task.data["action_id"] == os.environ.get("NOKIY_DEPLOYMENT_ACTION_ID"), "PARENT_PLAN_BINDING_REQUIRED")
        expected = {"schema_version": "nokiy_deployment_plan_v1", "action_id": task.data["action_id"],
                    "native_thread_id": task.data["native_thread_id"], "workspace": str(task.root),
                    "artifact_root": str(task.out / "artifacts"), "target": "nokiy-embedded-run", "release": task.new.name,
                    "expires_at": task.data["expires_at"]}
        require(all(plan.get(key) == value for key, value in expected.items())
                and set(plan["commands"]) == set(PHASES)
                and all(plan["commands"][name]["argv"] == task.argv(name) for name in PHASES), "PLAN_BINDING_DRIFT")
        for command in plan["commands"].values():
            for item in command["files"]:
                file_identity(item)
        require(plan["authorization_ref"] == identity(task.out / "authorization.json"), "AUTHORIZATION_DRIFT")
        self.check_pins()
        prepared = document(task.here / "preparation.json", limit=EVIDENCE_LIMIT)
        require(prepared["manifest_sha256"] == task.manifest_sha256 and prepared["input_hashes"] == task.hashes()
                and prepared["package"] == package_bytes(task.new / SITE / PACKAGE), "PREPARATION_DRIFT")
        require("paired_wire" in prepared, "PAIRED_WIRE_EXECUTION_REQUIRED")
        self.check_paired_wire(prepared["paired_wire"])
        require(self.lock_identity() == document(task.out / "lock-identity.json"), "INSTALLER_LOCK_DRIFT")
        task.installed(live=phase == "verify")
        marker = task.out / ("adapter-" + phase + ".started.json")
        require(not os.path.lexists(marker), "PHASE_ALREADY_ATTEMPTED", phase)
        if phase != "preflight":
            require((task.out / ("adapter-" + ("preflight" if phase == "apply" else "apply") + ".completed.json")).is_file(),
                    "PHASE_ORDER_INVALID", phase)
        save(marker, {"manifest_sha256": task.manifest_sha256, "plan_sha256": canonical(plan)})
        # Forward actual installer admission/readback, never manufacture success.
        try:
            returned = self.deploy.main(phase)
        except SystemExit as exit_status:
            require(exit_status.code is None or (type(exit_status.code) is int and exit_status.code == 0),
                    "INSTALLER_PHASE_FAILED", phase)
            returned = None
        require(returned is None or (type(returned) is int and returned == 0), "INSTALLER_PHASE_FAILED", phase)
        if phase == "verify" and "runtime_build" in task.data:
            require("live-tests" in self.runtime_tests_executed, "RUNTIME_TEST_EXECUTION_REQUIRED")
        task.hashes()
        self.check_pins()
        require(prepared["package"] == package_bytes(task.new / SITE / PACKAGE), "PREPARATION_DRIFT")
        task.installed(live=phase in ("apply", "verify"))
        save(task.out / ("adapter-" + phase + ".completed.json"), {"plan_sha256": canonical(plan)})


def run(task: Task, phase: str) -> dict[str, Any] | None:
    require(phase in ("prepare", *PHASES), "PHASE_INVALID")
    # Reject partial preparation before importing templates or executing any suite.
    if phase == "prepare":
        task.installed()
        require(not os.path.lexists(task.here) and not os.path.lexists(task.new), "PARTIAL_PREPARATION_EXISTS")
    else:
        require(absolute(str(task.here)).is_dir() and absolute(str(task.out)).is_dir(), "PREPARATION_REQUIRED")
        task.installed(live=phase == "verify")
    adapter = Adapter(task)
    if phase == "prepare":
        return adapter.prepare()
    adapter.phase(phase)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog="Manifest schema: " + SCHEMA +
        ". prepare only validates/stages and emits a plan; other phases require the parent's approved deployment lane.")
    parser.add_argument("--manifest", required=True, help="Explicit canonical task-local TASK.json")
    parser.add_argument("--manifest-sha256", required=True, help="Expected SHA-256 of exact manifest bytes")
    parser.add_argument("--phase", required=True, choices=("prepare", *PHASES))
    args = parser.parse_args(argv)
    try:
        result = run(Task.load(args.manifest, args.manifest_sha256), args.phase)
        if result is not None:
            print(json.dumps(result, sort_keys=True))
        return 0
    except PreparationError as error:
        code, detail = error.code, error.detail
    except FileExistsError as error:
        code, detail = "CREATE_ONLY_CONFLICT", str(error.filename or "")
    except Exception as error:
        code, detail = "BOUNDARY_FAILURE", type(error).__name__
    print(json.dumps({"schema_version": "nokiy_caller_release_error_v1", "code": code, "detail": detail,
                      "automatic_retry": False, "evidence_preserved": True}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
