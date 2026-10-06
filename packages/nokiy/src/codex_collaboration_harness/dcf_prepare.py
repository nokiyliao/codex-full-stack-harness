# SPDX-License-Identifier: MIT
"""One project process for exact DCF locators and canonical J-Space compilation."""
from __future__ import annotations

import json
from contextlib import ExitStack, closing
from pathlib import Path
import re
import sqlite3
import sys

TARGET = re.compile(r"symbol:[A-Za-z_][A-Za-z0-9_.]{0,254}")
PATH = re.compile(r"[A-Za-z0-9_./-]+")
MAX_BYTES = 262144


def targets_for(action):
    targets = action.get("navigation_targets")
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 4
            or any(not isinstance(t, str) or not TARGET.fullmatch(t) for t in targets)
            or len(set(targets)) != len(targets)
            or not isinstance(action.get("context_summary"), str)
            or not action["context_summary"].strip() or action.get("task_projection") is not None):
        raise ValueError("navigation requires 1..4 distinct exact symbols and a context summary")
    return targets


def bind_locators(action, locators, generation):
    targets = targets_for(action)
    if (not isinstance(generation, str) or not generation
            or not isinstance(locators, list) or len(locators) != len(targets)):
        raise ValueError("missing exact navigation evidence")
    for target, row in zip(targets, locators):
        if (not isinstance(row, dict) or row.get("target") != target
                or row.get("generation_id") != generation
                or not isinstance(row.get("path"), str) or not PATH.fullmatch(row["path"])
                or row["path"].startswith("/") or ".." in Path(row["path"]).parts
                or type(row.get("line")) is not int or row["line"] < 1):
            raise ValueError("navigation did not resolve an exact current locator")
    amended = dict(action)
    amended.pop("navigation_targets")
    amended["context_summary"] = (
        action["context_summary"].strip()
        + "\nDCF source-navigation locators (context only; no read/write grant):\n"
        + "\n".join(f"{row['target']} -> {row['path']}:{row['line']}" for row in locators)
    )
    return amended


def compile_bundle(runtime, surface_id, action):
    # Imports resolve inside the selected project, never a copied DCF implementation.
    from scripts.ops.dcf.graph import resolve_entities
    from scripts.ops.dcf.jspace import (
        compile_jspace_contract, compile_task_context_capsule, verify_contract_freshness,
    )

    targets = targets_for(action)
    snapshot, _, generation_dir = runtime.store.load_current(verify_file_hashes=True)
    _, capabilities, _ = runtime.capability_views(include_data=True, snapshot=snapshot)
    navigation = capabilities.get("source-navigation", {})
    if any(navigation.get(k) != v for k, v in (
            ("freshness_status", "current"), ("projection_status", "pass"), ("domain_verdict", "pass"))):
        raise ValueError("source-navigation is not current and passing")
    index, base_index = runtime.store.graph_paths(generation_dir, verify_file_hashes=True)
    locators = []
    with ExitStack() as opened:
        connection = opened.enter_context(closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)))
        base = (opened.enter_context(closing(sqlite3.connect(base_index.as_uri() + "?mode=ro", uri=True)))
                if base_index is not None else None)
        for target in targets:
            # Reuse DCF's overlay/base resolver, without traversing any edges.
            # A fuzzy match is not acceptable even when it has the right name.
            rows = resolve_entities(connection, target, base_connection=base, limit=2)
            if (not isinstance(rows, list) or len(rows) != 1 or rows[0]["entity_id"] != target
                    or rows[0]["entity_type"] != "symbol"):
                raise ValueError("symbol is absent, ambiguous or incomplete")
            locators.append({"target": target, "path": rows[0]["payload"]["path"],
                             "line": rows[0]["payload"]["line"], "generation_id": snapshot.generation_id})
    amended = bind_locators(action, locators, snapshot.generation_id)
    # Leave the canonical compiler's own snapshot and live-domain checks intact.
    # A generation change rejects the bundle instead of reusing stale grants.
    contract = compile_jspace_contract(runtime, surface_id=surface_id, action=amended)
    if contract["dcf_generation"]["generation_id"] != snapshot.generation_id:
        raise ValueError("DCF generation changed during compilation")
    verify_contract_freshness(runtime, contract)
    capsule = compile_task_context_capsule(contract, action=amended)
    return {"status": "compiled_not_admitted", "contract": contract,
            "task_context_capsule": capsule, "navigation_locators": locators,
            "action": amended, "authority_effect": "none", "no_apply": True}


def main():
    sys.path.insert(0, str(Path.cwd()))
    from scripts.ops.dcf.runtime import DcfRuntime
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("DCF preparation input exceeds budget")
    value = json.loads(raw)
    result = compile_bundle(DcfRuntime(Path.cwd()), value["surface_id"], value["action"])
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=True).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("DCF preparation output exceeds budget")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
