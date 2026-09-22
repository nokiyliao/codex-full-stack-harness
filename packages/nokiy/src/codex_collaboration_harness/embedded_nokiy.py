# SPDX-License-Identifier: MIT
"""One bounded Native Nokiy execution; the outer Codex owns continuation.

The existing Router owns admitted tool effects for one Native worker lifetime.
No Gateway, Session DB, persistent planner, callback or resume loop is started.
Immutable artifacts and effect receipts survive; model sessions do not. An
uncertain prior attempt is never re-executed automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


REQUEST_SCHEMA_VERSION = "tura_embedded_request_v3"
PREVIOUS_REQUEST_SCHEMA_VERSION = "tura_embedded_request_v2"
LEGACY_REQUEST_SCHEMA_VERSION = "tura_embedded_request_v1"
TERMINAL_SCHEMA_VERSION = "tura_embedded_terminal_v1"
PREFLIGHT_SCHEMA_VERSION = "tura_embedded_preflight_v1"
FAILURE_SCHEMA_VERSION = "tura_embedded_failure_v1"
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_SERVICE_TIER = "default"
SERVICE_TIERS = {"default", "priority", "ultrafast"}
MAX_REQUEST_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 512 * 1024
MAX_PROMPT_BYTES = 64 * 1024
MAX_TERMINAL_BYTES = 16 * 1024
MAX_RESULT_BYTES = 8 * 1024
MAX_TRAJECTORY_BYTES = 64 * 1024 * 1024
REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
AUTHORITY_EFFECTS = {"none", "workspace"}
REQUEST_ID_PATTERN = re.compile(r"^tura_embedded_[0-9a-f]{64}$")
THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
RUNTIME_IMAGE_SCHEMAS = {
    "tura-dcf-benchmark-v2-repair-runtime-image/v1",
    "tura-dcf-benchmark-frozen-full-runtime-image/v1",
}
REQUIRED_RUNTIME_ARTIFACTS = {
    "agent_prompt",
    "tura_router",
    "tura_native_codex_worker",
    "tura_command_graph",
}
NATIVE_COMMANDS = ("apply_patch", "bash", "shell_command", "zsh")
NATIVE_TERMINAL_SCHEMA = "tura_native_codex_terminal_envelope_v2"
REQUEST_KEYS = {
    "allow_provider_network",
    "artifact_root",
    "authority_effect",
    "codex",
    "context_capsule",
    "jspace_contract",
    "max_context_age_seconds",
    "max_result_bytes",
    "max_trajectory_bytes",
    "model",
    "model_acceleration",
    "native_thread_id",
    "persistence_mode",
    "prompt",
    "reasoning_effort",
    "require_tool_call",
    "runtime_image",
    "schema_version",
    "timeout_seconds",
    "workspace",
}
PREVIOUS_REQUEST_KEYS = REQUEST_KEYS - {"native_thread_id", "persistence_mode"}
LEGACY_REQUEST_KEYS = PREVIOUS_REQUEST_KEYS - {"require_tool_call"}
FILE_IDENTITY_KEYS = {"path", "sha256"}


class EmbeddedNokiyError(RuntimeError):
    """Typed fail-closed boundary for one embedded runtime request."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EmbeddedNokiyError(
                "NOKIY_EMBEDDED_DUPLICATE_JSON_KEY", f"duplicate key: {key}"
            )
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise EmbeddedNokiyError(
        "NOKIY_EMBEDDED_NONFINITE_JSON", f"invalid JSON constant: {value}"
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, *, limit: int, code: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise EmbeddedNokiyError(code, f"missing file: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EmbeddedNokiyError(code, f"not a plain file: {path}")
    if metadata.st_size > limit:
        raise EmbeddedNokiyError(code, f"file exceeds {limit} bytes: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EmbeddedNokiyError(code, f"invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise EmbeddedNokiyError(code, f"JSON root must be an object: {path}")
    return value


def _require_sha256(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", f"{name} must be lowercase SHA-256"
        )
    return value


def _plain_path(value: object, *, name: str, directory: bool) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", f"{name} must be an absolute path"
        )
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", f"{name} must be traversal-free"
        )
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", f"{name} does not exist"
        ) from error
    if stat.S_ISLNK(metadata.st_mode):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", f"{name} cannot be a symlink"
        )
    if directory and not resolved.is_dir():
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", f"{name} must be a directory"
        )
    if not directory and not resolved.is_file():
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", f"{name} must be a file"
        )
    return resolved


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: Path
    sha256: str

    @classmethod
    def decode(cls, value: object, *, name: str) -> "FileIdentity":
        if not isinstance(value, dict) or set(value) != FILE_IDENTITY_KEYS:
            raise EmbeddedNokiyError(
                "NOKIY_EMBEDDED_REQUEST_INVALID",
                f"{name} must contain exactly path and sha256",
            )
        return cls(
            path=_plain_path(value["path"], name=f"{name}.path", directory=False),
            sha256=_require_sha256(f"{name}.sha256", value["sha256"]),
        )

    def verify(self, *, code: str) -> None:
        if _file_sha256(self.path) != self.sha256:
            raise EmbeddedNokiyError(code, f"SHA-256 mismatch: {self.path}")

    def to_wire(self) -> dict[str, str]:
        return {"path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class EmbeddedNokiyRequest:
    runtime_image: FileIdentity
    codex: FileIdentity
    workspace: Path
    artifact_root: Path
    context_capsule: FileIdentity
    jspace_contract: FileIdentity
    prompt: str
    model: str
    reasoning_effort: str
    require_tool_call: bool
    native_thread_id: str | None
    persistence_mode: str
    timeout_seconds: int
    max_context_age_seconds: int
    max_trajectory_bytes: int
    max_result_bytes: int
    model_acceleration: bool
    allow_provider_network: bool
    authority_effect: str
    schema_version: str = REQUEST_SCHEMA_VERSION
    model_provider: str | None = None
    service_tier: str | None = None
    execution_profile: str | None = None
    request_id: str = field(init=False)
    request_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        payload = self.to_wire(include_identity=False)
        digest = _canonical_sha256(payload)
        object.__setattr__(self, "request_sha256", digest)
        object.__setattr__(self, "request_id", f"tura_embedded_{digest}")

    def to_wire(self, *, include_identity: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "runtime_image": self.runtime_image.to_wire(),
            "codex": self.codex.to_wire(),
            "workspace": str(self.workspace),
            "artifact_root": str(self.artifact_root),
            "context_capsule": self.context_capsule.to_wire(),
            "jspace_contract": self.jspace_contract.to_wire(),
            "prompt": self.prompt,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "require_tool_call": self.require_tool_call,
            "native_thread_id": self.native_thread_id,
            "persistence_mode": self.persistence_mode,
            "timeout_seconds": self.timeout_seconds,
            "max_context_age_seconds": self.max_context_age_seconds,
            "max_trajectory_bytes": self.max_trajectory_bytes,
            "max_result_bytes": self.max_result_bytes,
            "model_acceleration": self.model_acceleration,
            "allow_provider_network": self.allow_provider_network,
            "authority_effect": self.authority_effect,
        }
        if self.schema_version != REQUEST_SCHEMA_VERSION:
            value.pop("native_thread_id")
            value.pop("persistence_mode")
        if self.schema_version == LEGACY_REQUEST_SCHEMA_VERSION:
            value.pop("require_tool_call")
        if self.model_provider is not None:
            value["model_provider"] = self.model_provider
        if self.service_tier is not None:
            value["service_tier"] = self.service_tier
        if self.execution_profile is not None:
            value["execution_profile"] = self.execution_profile
        if include_identity:
            value.update(
                {"request_id": self.request_id, "request_sha256": self.request_sha256}
            )
        return value


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    image_sha256: str
    runtime_root: Path
    artifacts: Mapping[str, FileIdentity]
    build_identity: str


def decode_request(value: Mapping[str, Any]) -> EmbeddedNokiyRequest:
    schema_version = value.get("schema_version")
    if schema_version == REQUEST_SCHEMA_VERSION:
        expected_keys = REQUEST_KEYS
        value = dict(value)
        value.setdefault("model", DEFAULT_MODEL)
        value.setdefault("reasoning_effort", DEFAULT_REASONING_EFFORT)
        if "model_acceleration" not in value:
            value["model_acceleration"] = False
            value.setdefault("service_tier", DEFAULT_SERVICE_TIER)
    elif schema_version == PREVIOUS_REQUEST_SCHEMA_VERSION:
        expected_keys = PREVIOUS_REQUEST_KEYS
    elif schema_version == LEGACY_REQUEST_SCHEMA_VERSION:
        expected_keys = LEGACY_REQUEST_KEYS
    else:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", "unsupported schema_version"
        )
    optional_keys = {"model_provider", "service_tier", "execution_profile"} if schema_version == REQUEST_SCHEMA_VERSION else set()
    if not expected_keys <= set(value) or set(value) - expected_keys - optional_keys:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID",
            "request keys differ; "
            f"missing={sorted(expected_keys - set(value))}, "
            f"unknown={sorted(set(value) - expected_keys - optional_keys)}",
        )
    prompt = value.get("prompt")
    execution_profile = value.get("execution_profile")
    if "execution_profile" in value and (
        not isinstance(execution_profile, str) or execution_profile not in {"direct", "balanced"}
    ):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_REQUEST_INVALID", "execution_profile must be direct or balanced")
    if not isinstance(prompt, str) or not prompt.strip():
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", "prompt must be non-empty"
        )
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", "prompt exceeds compact input budget"
        )
    model = value.get("model")
    if not isinstance(model, str) or not model or "/" in model:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID",
            "model must be an official_codex_app_server model id",
        )
    model_provider = value.get("model_provider")
    if model_provider is not None and (
        not isinstance(model_provider, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", model_provider)
    ):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_REQUEST_INVALID", "invalid model_provider")
    service_tier = value.get("service_tier")
    if "service_tier" in value and (
        not isinstance(service_tier, str) or service_tier not in SERVICE_TIERS
    ):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_REQUEST_INVALID", "unsupported service_tier")
    reasoning = value.get("reasoning_effort")
    if reasoning not in REASONING_EFFORTS:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", "unsupported reasoning_effort"
        )
    timeout = value.get("timeout_seconds")
    context_age = value.get("max_context_age_seconds")
    trajectory_limit = value.get("max_trajectory_bytes")
    result_limit = value.get("max_result_bytes")
    for name, candidate, low, high in (
        ("timeout_seconds", timeout, 10, 900),
        ("max_context_age_seconds", context_age, 1, 7 * 24 * 60 * 60),
        ("max_trajectory_bytes", trajectory_limit, 1024, MAX_TRAJECTORY_BYTES),
        ("max_result_bytes", result_limit, 256, MAX_RESULT_BYTES),
    ):
        if type(candidate) is not int or not low <= candidate <= high:
            raise EmbeddedNokiyError(
                "NOKIY_EMBEDDED_REQUEST_INVALID",
                f"{name} must be an integer in [{low}, {high}]",
            )
    acceleration = value.get("model_acceleration")
    network = value.get("allow_provider_network")
    require_tool_call = value.get("require_tool_call", False)
    if (
        type(acceleration) is not bool
        or type(network) is not bool
        or type(require_tool_call) is not bool
    ):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID",
            "network, acceleration and require_tool_call must be bool",
        )
    effect = value.get("authority_effect")
    if effect not in AUTHORITY_EFFECTS:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_INVALID", "unsupported authority_effect"
        )
    native_thread_id = value.get("native_thread_id")
    persistence_mode = value.get("persistence_mode", "external_artifact_only")
    if schema_version == REQUEST_SCHEMA_VERSION:
        if not isinstance(native_thread_id, str) or not THREAD_ID_PATTERN.fullmatch(
            native_thread_id
        ):
            raise EmbeddedNokiyError(
                "NOKIY_EMBEDDED_REQUEST_INVALID",
                "native_thread_id must be a canonical Codex thread UUID",
            )
        if persistence_mode != "native_codex_thread_only":
            raise EmbeddedNokiyError(
                "NOKIY_EMBEDDED_REQUEST_INVALID",
                "v3 requires persistence_mode=native_codex_thread_only",
            )
    return EmbeddedNokiyRequest(
        runtime_image=FileIdentity.decode(value["runtime_image"], name="runtime_image"),
        codex=FileIdentity.decode(value["codex"], name="codex"),
        workspace=_plain_path(value["workspace"], name="workspace", directory=True),
        artifact_root=_plain_path(
            value["artifact_root"], name="artifact_root", directory=True
        ),
        context_capsule=FileIdentity.decode(
            value["context_capsule"], name="context_capsule"
        ),
        jspace_contract=FileIdentity.decode(
            value["jspace_contract"], name="jspace_contract"
        ),
        prompt=prompt,
        model=model,
        reasoning_effort=reasoning,
        require_tool_call=require_tool_call,
        native_thread_id=native_thread_id,
        persistence_mode=persistence_mode,
        timeout_seconds=timeout,
        max_context_age_seconds=context_age,
        max_trajectory_bytes=trajectory_limit,
        max_result_bytes=result_limit,
        model_acceleration=acceleration,
        allow_provider_network=network,
        authority_effect=effect,
        schema_version=schema_version,
        model_provider=model_provider,
        service_tier=service_tier,
        execution_profile=execution_profile,
    )


def load_request(path: Path) -> EmbeddedNokiyRequest:
    return decode_request(
        _load_json(path.resolve(strict=True), limit=MAX_REQUEST_BYTES, code="NOKIY_EMBEDDED_REQUEST_INVALID")
    )


def _verify_file_record(
    name: str, value: object, *, runtime_root: Path
) -> FileIdentity:
    if not isinstance(value, dict):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID", f"artifact {name} is invalid"
        )
    required = {"path", "sha256", "size"}
    if not required.issubset(value):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID", f"artifact {name} is incomplete"
        )
    identity = FileIdentity.decode(
        {"path": value["path"], "sha256": value["sha256"]},
        name=f"runtime_image.artifacts.{name}",
    )
    try:
        identity.path.relative_to(runtime_root)
    except ValueError as error:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID",
            f"artifact {name} escapes runtime_root",
        ) from error
    if type(value["size"]) is not int or identity.path.stat().st_size != value["size"]:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_DRIFT", f"artifact {name} size differs"
        )
    identity.verify(code="NOKIY_EMBEDDED_RUNTIME_IMAGE_DRIFT")
    if name.startswith("tura") and not os.access(identity.path, os.X_OK):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID",
            f"artifact {name} is not executable",
        )
    return identity


def verify_runtime_image(identity: FileIdentity, *, required_artifacts: set[str] | None = None) -> RuntimeIdentity:
    identity.verify(code="NOKIY_EMBEDDED_RUNTIME_IMAGE_DRIFT")
    image = _load_json(
        identity.path,
        limit=MAX_CONTEXT_BYTES,
        code="NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID",
    )
    if image.get("schema_version") not in RUNTIME_IMAGE_SCHEMAS:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID", "unsupported runtime image schema"
        )
    if image.get("status") != "FROZEN_BYTE_EXACT":
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID", "runtime image is not frozen"
        )
    runtime_root = _plain_path(
        image.get("runtime_root"), name="runtime_image.runtime_root", directory=True
    )
    raw_artifacts = image.get("artifacts")
    required = required_artifacts if required_artifacts is not None else REQUIRED_RUNTIME_ARTIFACTS
    if not isinstance(raw_artifacts, dict) or not required.issubset(
        raw_artifacts
    ):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID", "required runtime artifacts missing"
        )
    artifacts = {
        name: _verify_file_record(name, raw_artifacts[name], runtime_root=runtime_root)
        for name in sorted(raw_artifacts if required_artifacts is not None else required)
    }
    build = image.get("runtime_build_identity")
    if not isinstance(build, str) or not build:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RUNTIME_IMAGE_INVALID", "runtime build identity missing"
        )
    return RuntimeIdentity(identity.sha256, runtime_root, artifacts, build)


def _bound_json(identity: FileIdentity, *, code: str) -> dict[str, Any]:
    """Decode the same bytes whose file identity was verified, not a second read."""
    try:
        raw = identity.path.read_bytes()
        if len(raw) > MAX_CONTEXT_BYTES or hashlib.sha256(raw).hexdigest() != identity.sha256:
            raise EmbeddedNokiyError(code, "bound input bytes differ")
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (OSError, ValueError) as error:
        raise EmbeddedNokiyError(code, "bound input is not valid JSON") from error
    if not isinstance(value, dict):
        raise EmbeddedNokiyError(code, "bound input must be an object")
    return value


def _verify_context(request: EmbeddedNokiyRequest) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    context = _bound_json(request.context_capsule, code="NOKIY_EMBEDDED_CONTEXT_DRIFT")
    jspace = _bound_json(request.jspace_contract, code="NOKIY_EMBEDDED_JSPACE_DRIFT")
    if context.get("schema_version") != "task_context_capsule_v1":
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_INVALID", "unsupported context capsule schema"
        )
    context_payload = {key: item for key, item in context.items() if key != "semantic_sha256"}
    context_semantic = context.get("semantic_sha256")
    if context_semantic != _canonical_sha256(context_payload):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_INVALID", "context semantic digest differs"
        )
    generation = context.get("dcf_generation")
    if not isinstance(generation, dict) or not isinstance(
        generation.get("generated_at"), str
    ):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_INVALID", "context freshness metadata missing"
        )
    try:
        generated_at = datetime.fromisoformat(generation["generated_at"])
    except ValueError as error:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_INVALID", "context generated_at is invalid"
        ) from error
    if generated_at.tzinfo is None:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_INVALID", "context generated_at lacks timezone"
        )
    age = (datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)).total_seconds()
    local_context = generation.get("context_mode") == "local_workspace_jspace"
    action_scoped = (jspace.get("schema_version") == "jspace_contract_v2"
                     and not local_context
                     and "action_freshness" in generation)
    if age < -300 or (not action_scoped and age > request.max_context_age_seconds):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_STALE", f"context age {age:.0f}s exceeds budget"
        )
    def same_workspace(value: object) -> bool:
        if not isinstance(value, str) or not value:
            return False
        try:
            return Path(value).expanduser().resolve(strict=True) == request.workspace
        except OSError:
            return False

    surface = context.get("surface")
    if not isinstance(surface, dict) or not same_workspace(surface.get("repo_root")):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_SCOPE_MISMATCH", "context repo_root differs"
        )
    if not same_workspace(jspace.get("repo_root")):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_SCOPE_MISMATCH", "J-Space repo_root differs"
        )
    schema = jspace.get("schema_version")
    if schema == "jspace_contract_v1":
        semantic = jspace.get("semantic_sha256")
        expected = _canonical_sha256(
            {key: item for key, item in jspace.items() if key != "semantic_sha256"}
        )
    elif schema == "jspace_contract_v2":
        semantic = jspace.get("authorization_semantic_sha256")
        generation = jspace.get("dcf_generation")
        if not isinstance(generation, dict):
            raise EmbeddedNokiyError(
                "NOKIY_EMBEDDED_JSPACE_INVALID", "J-Space generation is invalid"
            )
        authorization_payload = {
            "schema_version": "jspace_authorization_v1",
            "repo_root": jspace.get("repo_root"),
            "required_domain_bindings": generation.get("required_domain_bindings"),
            "matched_surface_ids": jspace.get("matched_surface_ids"),
            "read_scopes": jspace.get("read_scopes"),
            "write_scopes": jspace.get("write_scopes"),
            "allowed_operations": jspace.get("allowed_operations"),
            "denied_operations": jspace.get("denied_operations"),
            "command_templates": jspace.get("command_templates"),
            "declared_targets": jspace.get("declared_targets"),
            "expansion": jspace.get("expansion"),
        }
        if "command_effect_policy" in jspace:
            policy = jspace["command_effect_policy"]
            if policy != "trusted_argv_effects_v1":
                raise EmbeddedNokiyError(
                    "NOKIY_EMBEDDED_JSPACE_INVALID", "unsupported command effect policy"
                )
            authorization_payload["command_effect_policy"] = policy
        if "read_commands" in jspace:
            authorization_payload["read_commands"] = jspace["read_commands"]
        expected = _canonical_sha256(authorization_payload)
        content = jspace.get("content_sha256")
        content_expected = _canonical_sha256(
            {key: item for key, item in jspace.items() if key != "content_sha256"}
        )
        if content != content_expected:
            raise EmbeddedNokiyError(
                "NOKIY_EMBEDDED_JSPACE_INVALID", "J-Space content digest differs"
            )
    else:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_JSPACE_INVALID", "unsupported J-Space schema"
        )
    if not isinstance(semantic, str) or semantic != expected:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_JSPACE_INVALID", "J-Space semantic digest differs"
        )
    if context.get("jspace_semantic_sha256") != semantic:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_CONTEXT_SCOPE_MISMATCH",
            "context and J-Space semantic identities differ",
        )
    write_scopes = jspace.get("write_scopes")
    if not isinstance(write_scopes, list) or not all(
        isinstance(item, str) for item in write_scopes
    ):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_JSPACE_INVALID", "write_scopes must be a string array"
        )
    if request.authority_effect == "none" and write_scopes:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_AUTHORITY_MISMATCH",
            "no-effect request cannot carry J-Space write scopes",
        )
    if action_scoped:
        if context["dcf_generation"] != jspace.get("dcf_generation"):
            raise EmbeddedNokiyError("NOKIY_EMBEDDED_CONTEXT_SCOPE_MISMATCH", "DCF generation bindings differ")
        from .full_stack import verify_action_freshness
        verify_action_freshness(request.workspace, jspace)
    if local_context:
        from .local_context import verify_context
        verify_context(request.workspace, context, jspace)
    return ({
        "context_semantic_sha256": str(context_semantic),
        "jspace_semantic_sha256": semantic,
        "dcf_generation_id": str(generation.get("generation_id", "")),
        "context_mode": "local_workspace_jspace" if local_context else "dcf_jspace_required",
    }, context, jspace)


def _verify_native_thread_binding(request: EmbeddedNokiyRequest) -> str | None:
    if request.schema_version != REQUEST_SCHEMA_VERSION:
        return None
    actual = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(actual, str) or not THREAD_ID_PATTERN.fullmatch(actual):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_NATIVE_THREAD_UNAVAILABLE",
            "current Native Codex thread identity is unavailable",
        )
    if actual != request.native_thread_id:
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_NATIVE_THREAD_MISMATCH",
            "request belongs to a different Native Codex thread",
        )
    return actual


def _native_request(request: EmbeddedNokiyRequest, runtime: RuntimeIdentity,
                    capsule: dict[str, Any], jspace: dict[str, Any]) -> dict[str, Any]:
    mission = capsule.get("mission")
    if not isinstance(mission, dict) or any(
        not isinstance(mission.get(key), str) or not mission[key].strip()
        for key in ("mission_id", "task_id", "mode", "objective", "current_predicate")
    ):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_CONTEXT_INVALID", "complete parent mission required")
    profile = {"model": request.model, "reasoning_effort": request.reasoning_effort,
               "service_tier": request.service_tier or (
                   "priority" if request.model_acceleration else "default")}
    if request.model_provider is not None:
        profile["model_provider"] = request.model_provider
    profile_digest = _canonical_sha256({
        "schema_version": "tura_native_execution_profile_v1", **profile,
        "balanced_prompt_sha256": runtime.artifacts["agent_prompt"].sha256,
        "sandbox": "read_only", "tool_surface": "tura_command_graph",
        "execution_model": "single_task",
    })
    delta = {"schema_version": "tura_native_codex_task_delta_v1",
             "mission_id": mission["mission_id"], "task_id": mission["task_id"],
             "mission_revision_sha256": _canonical_sha256(mission),
             "current_predicate": mission["current_predicate"], "instruction": request.prompt}
    delta["semantic_sha256"] = _canonical_sha256(delta)
    wire = {
        "schema_version": "tura_native_codex_worker_request_v3", "provider_profile": profile,
        "codex_executable": str(request.codex.path), "codex_executable_sha256": request.codex.sha256,
        "workspace": str(request.workspace), "session_id": f"once-{request.request_sha256}",
        "task_id": mission["task_id"], "execution_id": request.request_id,
        # This labels the disposable execution lifetime. It is NOT a replacement
        # for the outer task harness's writer lease or a grant of authority.
        "lease_id": f"execution-{request.request_sha256}",
        "execution_profile_sha256": profile_digest,
        "expected_task_context_capsule_sha256": capsule["semantic_sha256"],
        "expected_task_delta_sha256": delta["semantic_sha256"],
        "expected_jspace_semantic_sha256": capsule["jspace_semantic_sha256"],
        "task_context_capsule": capsule, "task_delta": delta,
        "sandbox": "read_only", "timeout_ms": request.timeout_seconds * 1000,
        "command_graph": {
            "executable": str(runtime.artifacts["tura_command_graph"].path),
            "executable_sha256": runtime.artifacts["tura_command_graph"].sha256,
            "allowed_commands": list(NATIVE_COMMANDS), "jspace_contract": jspace,
        },
    }
    wire["execution_binding_sha256"] = _canonical_sha256(wire)
    if len(_canonical_bytes(wire)) > 2 * 1024 * 1024:
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_REQUEST_INVALID", "Native input exceeds byte budget")
    return wire


def _prepare(request: EmbeddedNokiyRequest) -> tuple[dict[str, Any], RuntimeIdentity, dict[str, Any]]:
    if request.schema_version != REQUEST_SCHEMA_VERSION:
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_LEGACY_REQUEST_READBACK_ONLY",
                                "old requests remain readable; new execution requires the Native caller binding")
    request.codex.verify(code="NOKIY_EMBEDDED_CODEX_DRIFT")
    runtime = verify_runtime_image(request.runtime_image)
    context, capsule, jspace = _verify_context(request)
    native_thread_id = _verify_native_thread_binding(request)
    if not request.allow_provider_network:
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_PROVIDER_NETWORK_NOT_AUTHORIZED",
                                "provider execution requires explicit network authorization")
    wire = _native_request(request, runtime, capsule, jspace)
    ready = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION, "status": "READY",
        "request_id": request.request_id, "request_sha256": request.request_sha256,
        "runtime_image_sha256": runtime.image_sha256, "runtime_build_identity": runtime.build_identity,
        "codex_sha256": request.codex.sha256, "model_route": f"native_codex/{request.model}",
        "reasoning_effort": request.reasoning_effort, "service_tier": wire["provider_profile"]["service_tier"],
        "requested_service_tier": wire["provider_profile"]["service_tier"], "observed_service_tier": None,
        "requested_model_provider": request.model_provider, "observed_model_provider": None,
        "execution_profile_sha256": wire["execution_profile_sha256"],
        "tool_call_required": request.require_tool_call, "native_thread_id": native_thread_id,
        "persistence_mode": request.persistence_mode, "durable_session_owner": "native_codex_thread",
        "context": context, "ephemeral_gateway": False, "ephemeral_session_db": False,
        "execution_model": "single_task_native", "continuation_owner": "codex",
        "session_persistence": False, "native_codex_private_db_access": False,
        "credentials_read_by_bridge": False, "fallback_allowed": False,
    }
    return ready, runtime, wire


def preflight(request: EmbeddedNokiyRequest) -> dict[str, Any]:
    if request.execution_profile is not None:
        from .full_core import prepare
        return prepare(request)[0]
    return _prepare(request)[0]


def _direct_children(pid: int) -> set[int]:
    result = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
    )
    return {
        int(value)
        for value in result.stdout.split()
        if value.isascii() and value.isdigit()
    }


def _descendants(pid: int) -> set[int]:
    found: set[int] = set()
    pending = [pid]
    while pending:
        direct = _direct_children(pending.pop()) - found
        found.update(direct)
        pending.extend(direct)
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_process_group(
    process: subprocess.Popen[bytes], *, graceful_seconds: float = 10
) -> dict[str, Any]:
    descendants = _descendants(process.pid)
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=graceful_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
    deadline = time.monotonic() + 5
    unsettled = {pid for pid in descendants if _alive(pid)}
    while unsettled and time.monotonic() < deadline:
        time.sleep(0.1)
        unsettled = {pid for pid in unsettled if _alive(pid)}
    return {
        "process_stopped": process.poll() is not None,
        "observed_descendant_pids": sorted(descendants),
        "unsettled_descendant_pids": sorted(unsettled),
    }


def _record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve(strict=True)),
        "sha256": _file_sha256(path),
        "bytes": path.stat().st_size,
    }


def _write_create_only(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _canonical_bytes(value).decode("utf-8") + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _bounded_preview(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    marker = "\n...[bounded result truncated]...\n"
    marker_bytes = marker.encode("utf-8")
    remaining = max(0, limit - len(marker_bytes))
    head_budget = remaining * 2 // 3
    tail_budget = remaining - head_budget
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail = encoded[-tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
    return head + marker + tail, True


@contextmanager
def _cancellation_signals():
    cancelled: list[int] = []
    previous = {}

    def cancel(signum, _frame):
        cancelled.append(signum)

    # The CLI owns signal forwarding. Library calls on other threads leave
    # process-wide signal ownership with their host.
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, cancel)
    try:
        yield cancelled
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_process(
    command: list[str], *, cwd: Path, env: Mapping[str, str], stdin_path: Path,
    stdout_path: Path, stderr_path: Path, timeout: int, output_limit: int,
) -> tuple[int, float, str | None, dict[str, Any]]:
    started = time.monotonic()
    failure: str | None = None
    with _cancellation_signals() as cancelled, stdin_path.open("rb") as source, stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        if cancelled:
            raise EmbeddedNokiyError("NOKIY_EMBEDDED_CANCELLED", "cancelled before spawn")
        process = subprocess.Popen(command, cwd=cwd, env=dict(env), stdin=source,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            while process.poll() is None:
                if cancelled:
                    failure = "NOKIY_EMBEDDED_CANCELLED"
                    break
                if time.monotonic() - started >= timeout:
                    failure = "NOKIY_EMBEDDED_RUNTIME_TIMEOUT"
                    break
                if stdout_path.stat().st_size > output_limit:
                    failure = "NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED"
                    break
                if stderr_path.stat().st_size > 4 * 1024 * 1024:
                    failure = "NOKIY_EMBEDDED_STDERR_LIMIT_EXCEEDED"
                    break
                time.sleep(0.05)
        finally:
            # SIGTERM lets the Router settle its owned command and worker scopes.
            # Cleanup grace is not an additional provider execution budget.
            cleanup = _stop_process_group(process, graceful_seconds=0)
        if cancelled:
            failure = "NOKIY_EMBEDDED_CANCELLED"
        if stdout_path.stat().st_size > output_limit:
            failure = "NOKIY_EMBEDDED_TRAJECTORY_LIMIT_EXCEEDED"
        if stderr_path.stat().st_size > 4 * 1024 * 1024:
            failure = "NOKIY_EMBEDDED_STDERR_LIMIT_EXCEEDED"
        return process.wait(), time.monotonic() - started, failure, cleanup


def _verify_native_terminal(value: dict[str, Any], wire: dict[str, Any]) -> None:
    if value.get("schema_version") != NATIVE_TERMINAL_SCHEMA:
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_NATIVE_TERMINAL_INVALID", "Native terminal schema differs")
    bindings = {
        "task_id": "task_id", "execution_id": "execution_id", "lease_id": "lease_id",
        "execution_profile_sha256": "execution_profile_sha256",
        "execution_binding_sha256": "execution_binding_sha256",
        "task_context_capsule_sha256": "expected_task_context_capsule_sha256",
        "task_delta_sha256": "expected_task_delta_sha256",
    }
    if any(value.get(key) != wire[source] for key, source in bindings.items()):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_NATIVE_TERMINAL_BINDING_MISMATCH", "foreign terminal")
    if not (value.get("terminal") is True and value.get("process_reaped") is True
            and value.get("execution_lease_active") is False and value.get("ephemeral") is True
            and type(value.get("private_codex_state_write_count")) is int
            and value["private_codex_state_write_count"] == 0):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_NATIVE_LIFECYCLE_OPEN", "Native lifetime is not closed")
    if value.get("terminal_state") not in {"completed", "failed", "interrupted"}:
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_NATIVE_TERMINAL_INVALID", "unknown terminal state")
    for key in ("provider_input_sha256", "event_stream_sha256"):
        _require_sha256(key, value.get(key))
    text = value.get("final_text")
    if text is not None and (not isinstance(text, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != value.get("final_text_sha256")):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_NATIVE_RESULT_DRIFT", "final text digest differs")
    if value["terminal_state"] == "completed" and (value.get("process_exit_code") != 0 or value.get("error")):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_NATIVE_TERMINAL_INVALID", "failed execution claimed completion")
    observations = value.get("tool_observations", [])
    if not isinstance(observations, list) or any(not isinstance(row, dict)
            or row.get("tool_name") != "tura_command_graph"
            or row.get("item_type") != "mcp_tool_call"
            or type(row.get("is_error")) is not bool
            or row.get("status") not in {"completed", "failed"} for row in observations):
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_NATIVE_TOOL_OWNERSHIP_INVALID", "unexpected tool observation")


def execute(request: EmbeddedNokiyRequest) -> dict[str, Any]:
    if request.execution_profile is not None:
        from .full_core import execute_full_core
        return execute_full_core(request)
    _verify_native_thread_binding(request)
    run_root = request.artifact_root / request.request_id
    receipt_path = run_root / "terminal.json"
    if run_root.is_symlink():
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT", "run directory is a symlink")
    if receipt_path.is_file():
        return read_terminal(request.artifact_root, request.request_id)
    ready, runtime, wire = _prepare(request)
    try:
        run_root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_UNCERTAIN_PRIOR_ATTEMPT",
                                "request directory exists without a terminal receipt") from error
    _write_create_only(run_root / "request-identity.json", request.to_wire())
    native_request = run_root / "native-request.json"
    _write_create_only(native_request, wire)
    trajectory = run_root / "native-terminal.json"
    stderr_path = run_root / "runtime.stderr.log"
    last_message = run_root / "last-message.txt"
    env = dict(os.environ)
    for key in ("SESSION_LOG_DB_ROOT", "TURA_DB_ROOT", "TURA_GATEWAY_PID", "TURA_GATEWAY_PROCESS_START_TIME",
                "TURA_GATEWAY_URL", "TURA_ROUTER_ADDR", "TURA_SESSION_SERVICE_TIER", "TURA_SESSION_ACCELERATION_ENABLED"):
        env.pop(key, None)
    env.update({"PATH": f"{runtime.runtime_root}{os.pathsep}{env.get('PATH', '')}",
                "TURA_PROJECT_ROOT": str(runtime.runtime_root), "TURA_CWD": str(request.workspace),
                "TURA_RUNTIME_AUTO_GIT_COMMIT": "0", "FORCE_COLOR": "0"})
    command = [str(runtime.artifacts["tura_router"].path), "native-once", "--worker",
               str(runtime.artifacts["tura_native_codex_worker"].path), "--worker-sha256",
               runtime.artifacts["tura_native_codex_worker"].sha256]
    cleanup: dict[str, Any] = {"process_stopped": False, "unsettled_descendant_pids": []}
    native: dict[str, Any] = {}
    returncode, wall_time, first_blocker = -1, 0.0, None
    try:
        returncode, wall_time, first_blocker, cleanup = _run_process(
            command, cwd=request.workspace, env=env, stdin_path=native_request,
            stdout_path=trajectory, stderr_path=stderr_path,
            timeout=request.timeout_seconds, output_limit=request.max_trajectory_bytes)
        if first_blocker is None and returncode != 0:
            first_blocker = "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED"
        if first_blocker is None:
            candidate = _load_json(trajectory, limit=request.max_trajectory_bytes,
                                   code="NOKIY_EMBEDDED_NATIVE_TERMINAL_INVALID")
            _verify_native_terminal(candidate, wire)
            native = candidate
    except EmbeddedNokiyError as error:
        first_blocker = error.code
    except (OSError, subprocess.SubprocessError):
        first_blocker = "NOKIY_EMBEDDED_PROCESS_LIFETIME_UNPROVEN"
    cleanup["execution_scope_verified"] = bool(native) and returncode == 0
    cleanup["workspace_state_deleted"] = False
    cleanup_pass = (cleanup.get("process_stopped") is True
                    and not cleanup.get("unsettled_descendant_pids")
                    and cleanup["execution_scope_verified"] is True)
    result_text = native.get("final_text") or ""
    if result_text:
        with last_message.open("xb") as stream:
            stream.write(result_text.encode("utf-8"))
    preview, truncated = _bounded_preview(result_text, request.max_result_bytes)
    observations = native.get("tool_observations", [])
    tool_loop = {"observed": bool(observations), "started_count": None,
                 "completed_count": len(observations),
                 "successful_count": sum(row["status"] == "completed" and not row["is_error"]
                                         for row in observations),
                 "item_types": sorted({row["item_type"] for row in observations})}
    if first_blocker is None and native.get("terminal_state") != "completed":
        first_blocker = "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED"
    if first_blocker is None and not result_text:
        first_blocker = "NOKIY_EMBEDDED_RESULT_MISSING"
    if first_blocker is None and request.require_tool_call and tool_loop["successful_count"] == 0:
        first_blocker = "NOKIY_EMBEDDED_TOOL_LOOP_NOT_OBSERVED"
    if first_blocker is None and not cleanup_pass:
        first_blocker = "NOKIY_EMBEDDED_CLEANUP_UNPROVEN"
    receipt = {
        "schema_version": TERMINAL_SCHEMA_VERSION,
        "status": "RESULT_AVAILABLE" if first_blocker is None else "BLOCKED",
        "first_typed_blocker": first_blocker,
        "request_id": request.request_id, "request_sha256": request.request_sha256,
        "runtime_image_sha256": runtime.image_sha256, "runtime_build_identity": runtime.build_identity,
        "codex_sha256": request.codex.sha256, "model_route": ready["model_route"],
        "reasoning_effort": request.reasoning_effort, "service_tier": ready["service_tier"],
        "requested_service_tier": ready["requested_service_tier"], "observed_service_tier": None,
        "requested_model_provider": request.model_provider, "observed_model_provider": None,
        "execution_profile_sha256": wire["execution_profile_sha256"],
        "execution_id": wire["execution_id"], "session_id": wire["session_id"],
        "execution_model": "single_task_native", "continuation_owner": "codex",
        "mission_acceptance": "parent_owned", "tool_call_required": request.require_tool_call,
        "tool_loop": tool_loop, "native_thread_id": request.native_thread_id,
        "persistence_mode": request.persistence_mode, "durable_session_owner": "native_codex_thread",
        "result_text": preview, "result_truncated": truncated,
        "result_artifact": _record(last_message) if last_message.is_file() else None,
        "trajectory_artifact": _record(trajectory) if trajectory.is_file() else None,
        "native_request_artifact": _record(native_request),
        "stderr_artifact": _record(stderr_path) if stderr_path.is_file() else None,
        # Provider counters are observations, not recomputed billing totals.
        "usage": native.get("usage"), "wall_time_seconds": round(wall_time, 6),
        "runtime_exit_code": returncode, "cleanup": cleanup, "cleanup_pass": cleanup_pass,
        "authority_effect": request.authority_effect, "provider_network_authorized": request.allow_provider_network,
        "native_codex_private_db_access": False, "credentials_read_by_bridge": False,
        "native_codex_control_plane_mutation_count": 0, "session_persistence": False,
        "fallback_used": False, "replayable_terminal": True, "preflight_sha256": _canonical_sha256(ready),
    }
    if len(_canonical_bytes(receipt)) + 1 > MAX_TERMINAL_BYTES:
        receipt["result_text"] = ""
        receipt["result_truncated"] = bool(result_text)
    if len(_canonical_bytes(receipt)) + 1 > MAX_TERMINAL_BYTES:
        raise EmbeddedNokiyError("NOKIY_EMBEDDED_TERMINAL_TOO_LARGE", "terminal exceeds byte budget")
    _write_create_only(receipt_path, receipt)
    return receipt


def read_terminal(artifact_root: Path, request_id: str) -> dict[str, Any]:
    """Read a completed result without revalidating expired execution inputs."""

    root = _plain_path(artifact_root, name="artifact_root", directory=True)
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_REQUEST_ID_INVALID", "request_id is malformed"
        )
    receipt = _load_json(
        root / request_id / "terminal.json",
        limit=MAX_TERMINAL_BYTES,
        code="NOKIY_EMBEDDED_RECEIPT_INVALID",
    )
    if (
        receipt.get("schema_version") != TERMINAL_SCHEMA_VERSION
        or receipt.get("request_id") != request_id
        or receipt.get("request_sha256") != request_id.removeprefix("tura_embedded_")
    ):
        raise EmbeddedNokiyError(
            "NOKIY_EMBEDDED_RECEIPT_IDENTITY_MISMATCH",
            "terminal identity cannot be recomputed",
        )
    return receipt


def _failure(error: EmbeddedNokiyError) -> dict[str, Any]:
    return {
        "schema_version": FAILURE_SCHEMA_VERSION,
        "status": "BLOCKED_PREEXECUTION",
        "first_typed_blocker": error.code,
        "detail_sha256": hashlib.sha256(error.detail.encode("utf-8")).hexdigest(),
        "authority_effect": "none",
        "native_codex_control_plane_mutation_count": 0,
        "fallback_used": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one bounded Native Nokiy task with Router-owned tools. "
            "Return a terminal; only the outer Codex decides continuation."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Compile fresh DCF + J-Space for a new Direct request")
    prepare.add_argument("--request", required=True, help="New request draft without context identities")
    prepare.add_argument("--action", required=True, help="Bounded DCF action JSON, including mission")
    prepare.add_argument("--surface-id", help="Required for DCF workspaces; omit for local context")
    prepare.add_argument("--output-dir", required=True, help="New absolute preparation directory")
    for name in ("preflight", "run"):
        command = commands.add_parser(name)
        command.add_argument("--request", required=True)
    read = commands.add_parser("read-result")
    read.add_argument("--artifact-root", required=True)
    read.add_argument("--request-id", required=True)
    for name in ("check-deployment", "deploy"):
        deploy = commands.add_parser(name, help="Execute exact parent-admitted deployment commands without a model")
        deploy.add_argument("--plan", required=True)
        deploy.add_argument("--approved-plan-sha256", required=True)
    deployed = commands.add_parser("read-deployment-result")
    deployed.add_argument("--artifact-root", required=True)
    deployed.add_argument("--action-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command in {"check-deployment", "deploy", "read-deployment-result"}:
            from . import deployment
            result = (deployment.read_result(Path(arguments.artifact_root), arguments.action_id)
                      if arguments.command == "read-deployment-result" else
                      deployment.execute(Path(arguments.plan), arguments.approved_plan_sha256,
                                         check_only=arguments.command == "check-deployment"))
        elif arguments.command == "prepare":
            from .full_stack import prepare_request
            result = prepare_request(Path(arguments.request), Path(arguments.action),
                                     arguments.surface_id, Path(arguments.output_dir))
        elif arguments.command == "read-result":
            result = read_terminal(Path(arguments.artifact_root), arguments.request_id)
        else:
            request = load_request(Path(arguments.request))
            result = (
                preflight(request)
                if arguments.command == "preflight"
                else execute(request)
            )
    except (OSError, ValueError) as error:
        print(json.dumps(_failure(EmbeddedNokiyError("NOKIY_EMBEDDED_INPUT_ERROR", str(error))),
                         ensure_ascii=True, sort_keys=True))
        return 2
    except EmbeddedNokiyError as error:
        print(json.dumps(_failure(error), ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") in {"PREPARED", "READY", "RESULT_AVAILABLE", "VERIFIED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
