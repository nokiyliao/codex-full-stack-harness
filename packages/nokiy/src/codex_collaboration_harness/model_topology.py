# SPDX-License-Identifier: MIT
"""Local model-picker selection, NOT an inference provider or authority issuer.

The native host must own root selection, delegation interception and lifecycle.
This module implements the worker-side contract and a non-installing catalog
builder. A family in the reasoning UI is never a provider reasoning effort.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from . import embedded_nokiy as caller

MANIFEST = Path(__file__).with_name("protocol") / "nokiy_model_topology_v1.json"
SCHEMA = "nokiy_model_selection_v1"
BINDING_SCHEMA = "nokiy_prepared_model_selection_v1"
MODES = ("nokiy", "nokiy-direct")
FAMILIES = ("luna", "sol", "astra")
WORKER_MODELS = {"luna": "gpt-6-luna", "sol": "gpt-6.1-sol", "astra": "gpt-6-astra"}


def _fail(code: str, message: str) -> None:
    raise caller.EmbeddedNokiyError("NOKIY_TOPOLOGY_" + code, message)


def manifest() -> dict[str, Any]:
    value = caller._load_json(MANIFEST, limit=8192, code="NOKIY_TOPOLOGY_MANIFEST_INVALID")
    if (value.get("schema_version") != "nokiy_model_topology_v1"
            or set(value.get("worker_families", {})) != set(FAMILIES)
            or set(value.get("modes", {})) != set(MODES)
            or value.get("default_worker_family") not in FAMILIES
            or value.get("worker_delegation_allowed") is not False
            or value.get("selection_is_authorization") is not False):
        _fail("MANIFEST_INVALID", "invalid topology contract")
    return value


def resolve(mode: str, family: str | None = None) -> dict[str, Any]:
    spec = manifest()
    if not isinstance(mode, str) or mode not in MODES:
        _fail("MODE_INVALID", "mode must be nokiy or nokiy-direct")
    if family is None:
        family = spec["default_worker_family"]
    if not isinstance(family, str) or family not in FAMILIES:
        _fail("FAMILY_INVALID", "worker family must be luna, sol or astra")
    configured = spec["modes"][mode]
    worker = deepcopy(spec["worker_families"][family])
    if worker != {"model": WORKER_MODELS[family], "reasoning_effort": "max"}:
        _fail("MANIFEST_INVALID", "workers must be explicit supported families at max")
    commander = deepcopy(configured["commander"])
    expected = None if mode == "nokiy" else {"model": "gpt-6-astra", "reasoning_effort": "ultra"}
    expected_root = "nokiy" if mode == "nokiy" else "native_codex"
    if (commander != expected or spec.get("worker_execution_profile") != "direct"
            or configured.get("root_executor") != expected_root):
        _fail("MANIFEST_INVALID", "commander, root or execution profile drift")
    return {
        "schema_version": SCHEMA,
        "selected_model": mode,
        "worker_family": family,
        "commander": commander,
        "worker": worker,
        "root_executor": configured["root_executor"],
        "worker_execution_profile": "direct",
        "worker_delegation_allowed": False,
        "selection_is_authorization": False,
    }


def require_catalog_support(selection: dict, catalog: dict) -> None:
    rows = catalog.get("models")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        _fail("CATALOG_INVALID", "a nonempty model catalog is required")
    ids = [row.get("slug") for row in rows]
    if any(not isinstance(name, str) or not name for name in ids) or len(set(ids)) != len(ids):
        _fail("CATALOG_INVALID", "model identities must be unique nonempty strings")
    by_id = dict(zip(ids, rows))
    required = [selection["worker"]]
    if selection["commander"] is not None:
        required.append(selection["commander"])
    for identity in required:
        row = by_id.get(identity["model"])
        levels = row.get("supported_reasoning_levels", []) if row else []
        if not isinstance(levels, list) or not any(
                isinstance(level, dict) and level.get("effort") == identity["reasoning_effort"]
                for level in levels):
            _fail("MODEL_UNAVAILABLE", "catalog lacks the requested model/effort: " + identity["model"])


def project_worker_draft(draft: dict, mode: str, family: str | None) -> tuple[dict, dict]:
    """Select a worker without changing scope, tier, network or ownership grants.

    Existing explicit contradictory settings are errors, never silent overrides.
    This is also usable by today's manual skill; it does not attest the root.
    """
    selection = resolve(mode, family)
    expected = {**selection["worker"], "execution_profile": "direct"}
    for key, value in expected.items():
        if key in draft and draft[key] != value:
            _fail("REQUEST_CONFLICT", "explicit draft " + key + " conflicts with model selection")
    for key in ("context_capsule", "jspace_contract", "request_id", "request_sha256", "model_selection"):
        if key in draft:
            _fail("REQUEST_CONFLICT", "a submitted request cannot be reselected")
    if draft.get("model_provider") not in (None, "openai"):
        _fail("REQUEST_CONFLICT", "an alternate provider is not part of this topology")
    result = deepcopy(draft)
    result.update(expected)
    return result, selection


def request_marker(selection: dict) -> dict[str, str]:
    return {"schema_version": SCHEMA, "selected_model": selection["selected_model"],
            "worker_family": selection["worker_family"]}


def validate_request_marker(marker: object, model: str, effort: str,
                            profile: str | None) -> dict:
    if (not isinstance(marker, dict) or set(marker) !=
            {"schema_version", "selected_model", "worker_family"}
            or marker.get("schema_version") != SCHEMA
            or marker.get("selected_model") not in MODES
            or marker.get("worker_family") not in FAMILIES):
        _fail("REQUEST_MARKER_INVALID", "invalid request model-selection marker")
    selection = resolve(marker["selected_model"], marker["worker_family"])
    if {"model": model, "reasoning_effort": effort} != selection["worker"] or profile != "direct":
        _fail("REQUEST_MARKER_INVALID", "request worker differs from model selection")
    return selection


def bind_prepared_request(selection: dict, request: caller.EmbeddedNokiyRequest) -> dict:
    """Bind the exact create-only request encoding before publishing request.json.

    This is integrity, not authorization; the native host remains the authority.
    """
    request_bytes = caller._canonical_bytes(request.to_wire(include_identity=False)) + b"\n"
    if ({"model": request.model, "reasoning_effort": request.reasoning_effort} != selection["worker"]
            or request.execution_profile != "direct" or request.model_selection != request_marker(selection)):
        _fail("REQUEST_CONFLICT", "prepared request differs from selected worker")
    return {
        "schema_version": BINDING_SCHEMA,
        "selection": selection,
        "selection_sha256": caller._canonical_sha256(selection),
        "request_id": request.request_id,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "native_thread_id": request.native_thread_id,
        "root_topology_verified": False,
        "provider_observed_model": None,
        "note": "Worker selection only. Native root/delegation admission is separate.",
    }


def load_prepared_request(request_path: Path) -> tuple[caller.EmbeddedNokiyRequest, dict | None]:
    """Load one request snapshot and validate its required selection before execution.

    read-result intentionally does not call this: historical recovery is bound
    to the original request, not the model picker currently selected in the UI.
    """
    value, request_bytes = caller._load_json_snapshot(
        request_path, limit=caller.MAX_REQUEST_BYTES, code="NOKIY_EMBEDDED_REQUEST_INVALID")
    return decode_prepared_snapshot(request_path, value, request_bytes)


def decode_prepared_snapshot(request_path: Path, value: dict,
                             request_bytes: bytes) -> tuple[caller.EmbeddedNokiyRequest, dict | None]:
    """Validate the same bounded snapshot already read by a batch; never reread it."""
    request = caller.decode_request(value)
    marker = request.model_selection
    binding_path = request_path.parent / "model-selection.json"
    preparation_path = request_path.parent / "preparation.json"
    declared_binding = None
    if preparation_path.exists() or preparation_path.is_symlink():
        if preparation_path.is_symlink():
            _fail("BINDING_INVALID", "preparation must be a physical file")
        preparation = caller._load_json(preparation_path, limit=2 * 1024 * 1024,
                                        code="NOKIY_TOPOLOGY_BINDING_INVALID")
        declared_binding = preparation.get("model_selection")
    if not binding_path.exists() and not binding_path.is_symlink():
        if marker is not None or declared_binding is not None:
            _fail("BINDING_INVALID", "preparation requires its original model-selection companion")
        return request, None  # Existing caller requests keep their original behavior.
    if marker is None or declared_binding is None:
        _fail("BINDING_INVALID", "selection needs its request marker and preparation declaration")
    if binding_path.is_symlink() or request_path.is_symlink():
        _fail("BINDING_INVALID", "request and selection must be physical files")
    binding, binding_bytes = caller._load_json_snapshot(
        binding_path, limit=16384, code="NOKIY_TOPOLOGY_BINDING_INVALID")
    if declared_binding != {"path": str(binding_path.resolve()),
                            "sha256": hashlib.sha256(binding_bytes).hexdigest()}:
        _fail("BINDING_INVALID", "preparation/selection binding drift")
    selection = binding.get("selection")
    if not isinstance(selection, dict):
        _fail("BINDING_INVALID", "selection is missing")
    expected = validate_request_marker(marker, request.model, request.reasoning_effort,
                                       request.execution_profile)
    if (selection != expected or binding.get("schema_version") != BINDING_SCHEMA
            or binding.get("selection_sha256") != caller._canonical_sha256(selection)
            or binding.get("request_sha256") != hashlib.sha256(request_bytes).hexdigest()):
        _fail("BINDING_INVALID", "selection or prepared request bytes changed")
    checked = bind_prepared_request(selection, request)
    if binding != checked:
        _fail("BINDING_INVALID", "prepared selection identity differs")
    return request, selection


def verify_prepared_selection(request_path: Path) -> dict | None:
    """Compatibility wrapper for callers needing only the verified selection."""
    return load_prepared_request(request_path)[1]


def build_candidate_catalog(catalog: dict) -> dict:
    """Return a separate candidate catalog. Never edit or admit a live catalog.

    Native instruction/tool templates still need host-level topology projection.
    A parsed/listed alias is not proof that dispatch works.
    """
    for mode in MODES:
        for family in FAMILIES:
            require_catalog_support(resolve(mode, family), catalog)
    if any(row["slug"] in MODES for row in catalog["models"]):
        _fail("CATALOG_CONFLICT", "candidate entries already exist; do not append duplicates")
    result = deepcopy(catalog)
    base = {row["slug"]: row for row in catalog["models"]}
    families = [base[WORKER_MODELS[name]] for name in FAMILIES]
    spec = manifest()
    for mode in MODES:
        anchor = base[WORKER_MODELS["astra" if mode == "nokiy-direct" else "sol"]]
        row = deepcopy(anchor)
        row.update(
            slug=mode,
            display_name=spec["modes"][mode]["display_name"],
            description=("LOCAL TOPOLOGY CANDIDATE; requires native host adapter. "
                         "Reasoning selector chooses the Nokiy worker family, always at max."),
            default_reasoning_level="sol",
            supported_reasoning_levels=[
                {"effort": family, "description": family.title() + " / Max — Nokiy worker"}
                for family in FAMILIES
            ],
            visibility="list",
            upgrade=None,
            availability_nux=None,
        )
        # The menu family is not a provider capacity. Use the conservative bound
        # until the native host selects the real model's complete metadata.
        for key in ("context_window", "max_context_window", "auto_compact_token_limit"):
            values = [item.get(key) for item in families]
            if all(type(value) is int and value > 0 for value in values):
                row[key] = min(values)
        result["models"].append(row)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--model", choices=MODES, required=True)
    plan.add_argument("--worker-family", choices=FAMILIES, default="sol")
    catalog = commands.add_parser("catalog-candidate")
    catalog.add_argument("--catalog", type=Path, required=True)
    catalog.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = {"status": "RESOLVED_NOT_EXECUTED", "selection": resolve(args.model, args.worker_family)}
        else:
            source = caller._load_json(args.catalog, limit=16 * 1024 * 1024,
                                       code="NOKIY_TOPOLOGY_CATALOG_INVALID")
            candidate = build_candidate_catalog(source)
            if not args.output.is_absolute() or args.output == args.catalog or args.output.is_symlink():
                _fail("OUTPUT_INVALID", "a new absolute candidate path is required")
            caller._write_create_only(args.output, candidate)
            result = {"status": "CANDIDATE_NOT_ADMITTED", "catalog": str(args.output),
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      "added_models": list(MODES), "live_catalog_modified": False}
    except (caller.EmbeddedNokiyError, OSError, ValueError) as error:
        print(json.dumps({"status": "BLOCKED", "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
