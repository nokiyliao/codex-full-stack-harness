# SPDX-License-Identifier: MIT
"""Opt-in, exact Python function context after J-Space grants a file read."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import stat

from . import embedded_nokiy as caller


CODE = "NOKIY_SOURCE_EXCERPT_UNVERIFIED"
MAX_SOURCE_BYTES = 1_048_576
MAX_EXCERPT_BYTES = 12_288
MAX_EXCERPT_LINES = 120
MAX_ENVELOPE_BYTES = 2 * MAX_EXCERPT_BYTES
_ENVELOPE_ALLOCATION_MODE = "envelope_bounded"
MAX_SUPPORT_LOCATORS = 32
MAX_SUPPORT_LOCATOR_BYTES = 4096
SUPPORT_NAVIGATION_KIND = "same_file_syntactic_candidates_not_dependency_resolution"
CONTEXT_MARKER = "\nExact J-Space-authorized source excerpt (context only; not a new grant):\n"
EDIT_NOTICE = (
    "Pre-edit source only: complete selected definitions and conservative same-file "
    "syntactic support, not complete files or dependency resolution. After mutation, "
    "use ordinary source_read for fresh readback; this preimage is not post-edit verification."
)
PARTIAL_EDIT_NOTICE = (
    "Pre-edit definitions-only context: selected complete target definitions (including "
    "decorators), NOT complete files or dependency support. Missing syntactic support "
    "and deferred target locators require ordinary source_read. After mutation, use "
    "ordinary source_read for fresh readback; this preimage is not post-edit verification."
)


class ExcerptBudgetExceeded(caller.EmbeddedNokiyError):
    """An admitted source excerpt exceeds capacity, not an authority refusal."""


def _exact_source_path(name: str) -> bool:
    if not isinstance(name, str) or not name or any(char in name for char in "*?[]\x00"):
        return False
    relative = Path(name)
    return (not relative.is_absolute() and relative.as_posix() == name and bool(relative.parts)
            and not any(part in {".", "..", ".git"} for part in relative.parts))


def _exact_python_path(name: str) -> bool:
    return _exact_source_path(name) and Path(name).suffix == ".py"


def _read_exact(workspace: Path, name: str, *, python_only: bool = True) -> bytes:
    if not (_exact_python_path(name) if python_only else _exact_source_path(name)):
        description = "Python source" if python_only else "source"
        raise caller.EmbeddedNokiyError(CODE, f"excerpt requires one exact {description} path")
    relative = Path(name)
    directory = None
    source = None
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        directory = os.open(workspace, flags | os.O_DIRECTORY)
        for part in relative.parts[:-1]:
            next_directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        source = os.open(relative.parts[-1], flags, dir_fd=directory)
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES:
            raise caller.EmbeddedNokiyError(CODE, "source is not a bounded regular file")
        chunks = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(source, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(source)
        def identity(info):
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        if len(data) != before.st_size or identity(before) != identity(after):
            raise caller.EmbeddedNokiyError(CODE, "source changed during excerpt read")
        return data
    except (OSError, ValueError) as error:
        raise caller.EmbeddedNokiyError(CODE, "exact source read failed") from error
    finally:
        if source is not None:
            os.close(source)
        if directory is not None:
            os.close(directory)


def _supporting_ranges(tree: ast.Module, target: ast.AST) -> list[tuple[int, int]]:
    """Conservative syntactic support, not scope resolution or dependency proof.

    Only direct module bindings are followed. Shadowing can over-select; dynamic
    lookups, wildcard imports and conditional bindings are not resolved.
    """
    bindings = {}
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [part.id for item in targets for part in ast.walk(item)
                     if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store)]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or (alias.name.split(".")[0]
                     if isinstance(node, ast.Import) else alias.name)
                     for alias in node.names if alias.name != "*"]
        for name in names:
            bindings.setdefault(name, []).append(node)

    pending = [target]
    seen = {target}
    followed_names = set()
    ranges = []
    # Include decorators as support without changing the target's locator line.
    if target.decorator_list:
        ranges.append((min(item.lineno for item in target.decorator_list), target.lineno - 1))
    while pending:
        node = pending.pop()
        references = {part.id for part in ast.walk(node)
                      if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load)}
        for name in sorted(references - followed_names):
            followed_names.add(name)
            for support in bindings.get(name, ()):
                if support in seen:
                    continue
                seen.add(support)
                pending.append(support)
                start = min([support.lineno] + [item.lineno for item in
                            getattr(support, "decorator_list", ())])
                ranges.append((start, support.end_lineno))

    # Merge overlaps, then remove the target's lines, even for nested targets.
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    result = []
    for start, end in merged:
        if end < target.lineno or start > target.end_lineno:
            result.append((start, end))
        else:
            if start < target.lineno:
                result.append((start, target.lineno - 1))
            if end > target.end_lineno:
                result.append((target.end_lineno + 1, end))
    return result


def extract(workspace: Path, contract: dict, locator: dict, *,
            include_dependencies: bool = False) -> dict:
    """Return exact source, optionally with bounded same-file syntactic support."""
    name = locator["path"]
    operations = contract.get("allowed_operations", [])
    bounded_read = (
        isinstance(operations, list)
        and isinstance(contract.get("read_scopes"), list)
        and all(operation in ("read", "command") for operation in operations)
        # focused_verifiers is DCF evidence, not an execution capability.
        and not any(contract.get(key) for key in
                    ("verifier_commands", "verifier_grants", "verifier_command_templates"))
    )
    if (contract.get("write_scopes") != [] or "read" not in contract.get("allowed_operations", [])
            or (include_dependencies and not bounded_read)
            or ("command" in operations and not (
                include_dependencies and bounded_read and contract.get("source_read") is True))
            or "read" in contract.get("denied_operations", [])
            or name not in contract.get("read_scopes", [])
            or contract.get("command_templates")
            or contract.get("dcf_generation", {}).get("generation_id") != locator["generation_id"]):
        raise caller.EmbeddedNokiyError(CODE, "read-only exact J-Space grant and generation required")
    data = _read_exact(workspace, name)
    try:
        source = data.decode("utf-8")
        tree = ast.parse(source)
    except (UnicodeError, SyntaxError) as error:
        raise caller.EmbeddedNokiyError(CODE, "source cannot be parsed as Python") from error
    line = locator["line"]
    nodes = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and node.lineno == line]
    if len(nodes) != 1 or nodes[0].end_lineno is None:
        raise caller.EmbeddedNokiyError(CODE, "DCF line does not identify one complete definition")
    end = nodes[0].end_lineno
    lines = source.splitlines(keepends=True)
    excerpt = "".join(lines[line - 1:end])
    if end - line + 1 > MAX_EXCERPT_LINES or len(excerpt.encode("utf-8")) > MAX_EXCERPT_BYTES:
        raise ExcerptBudgetExceeded(CODE, "definition exceeds the bounded excerpt budget")
    result = {"path": name, "start_line": line, "end_line": end,
              "source_sha256": hashlib.sha256(data).hexdigest(), "text": excerpt}
    if include_dependencies:
        spans = []
        line_count = end - line + 1
        byte_count = len(excerpt.encode("utf-8"))
        for start, stop in _supporting_ranges(tree, nodes[0]):
            text = "".join(lines[start - 1:stop])
            line_count += stop - start + 1
            byte_count += len(text.encode("utf-8"))
            if line_count > MAX_EXCERPT_LINES or byte_count > MAX_EXCERPT_BYTES:
                raise ExcerptBudgetExceeded(CODE, "support exceeds the bounded excerpt budget")
            spans.append({"start_line": start, "end_line": stop, "text": text})
        result["supporting_spans"] = spans
    return result


def _edit_locators(contract: dict, locators: list) -> list[dict]:
    """Validate all authority/navigation before opening even the first source file."""
    from .dcf_prepare import PATH, TARGET

    generation_data = contract.get("dcf_generation")
    generation = generation_data.get("generation_id") if isinstance(generation_data, dict) else None
    if (not isinstance(generation, str) or not generation
            or not isinstance(locators, list) or not 1 <= len(locators) <= 4):
        raise caller.EmbeddedNokiyError(CODE, "1..4 exact current edit locators required")
    targets = {}
    for row in locators:
        if (not isinstance(row, dict)
                or set(row) != {"target", "path", "line", "generation_id"}
                or not isinstance(row["target"], str) or not TARGET.fullmatch(row["target"])
                or not _exact_python_path(row["path"]) or not PATH.fullmatch(row["path"])
                or type(row["line"]) is not int or row["line"] < 1
                or row["generation_id"] != generation):
            raise caller.EmbeddedNokiyError(CODE, "edit navigation is not exact and current")
        previous = targets.setdefault(row["target"], row)
        if previous != row:
            raise caller.EmbeddedNokiyError(CODE, "edit navigation is ambiguous")
    ordered = sorted(targets.values(), key=lambda row: (row["path"], row["line"], row["target"]))
    names = {row["path"] for row in ordered}
    operations = contract.get("allowed_operations")
    denied = contract.get("denied_operations", [])
    if (not isinstance(operations, list) or not all(isinstance(op, str) for op in operations)
            or not {"read", "modify"} <= set(operations) <= {"read", "modify", "command"}
            or not isinstance(denied, list) or any(op in denied for op in operations)
            or contract.get("source_read") is not True):
        raise caller.EmbeddedNokiyError(CODE, "edit excerpts require existing read/modify and source_read capabilities")
    for key in ("read_scopes", "write_scopes"):
        scopes = contract.get(key)
        if (not isinstance(scopes, list) or not all(isinstance(path, str) for path in scopes)
                or len(scopes) != len(set(scopes)) or not names <= set(scopes)):
            raise caller.EmbeddedNokiyError(CODE, "each edit excerpt requires an exact read and write grant")
    return [dict(row) for row in ordered]


def _direct_support_locators(tree: ast.Module, target: ast.AST, name: str) -> list[dict]:
    """Direct syntactic candidates only; no imports, dynamic or recursive resolution."""
    bindings = {}
    owner = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.setdefault(node.name, []).append(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and target in node.body:
            owner = node
            break
    found = {}
    references = {part.id for part in ast.walk(target)
                  if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load)}
    for reference in sorted(references):
        for node in bindings.get(reference, ()):
            found[(node.lineno, reference)] = node
    if owner is not None:
        methods = {}
        for node in owner.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.setdefault(node.name, []).append(node)
        for part in ast.walk(target):
            if (isinstance(part, ast.Attribute) and isinstance(part.value, ast.Name)
                    and part.value.id in {"self", "cls"}):
                for node in methods.get(part.attr, ()):
                    found[(node.lineno, owner.name + "." + part.attr)] = node
    return [{"path": name, "name": symbol,
             "kind": "class" if isinstance(node, ast.ClassDef) else "function",
             "start_line": min([node.lineno] + [item.lineno for item in node.decorator_list]),
             "end_line": node.end_lineno}
            for (_, symbol), node in sorted(found.items())]


def _add_support_navigation(excerpt: dict, candidates: list[dict]) -> dict:
    files = {row["path"]: row for row in excerpt["files"]}
    rows = []
    seen = set()
    for row in sorted(candidates, key=lambda item: (item["path"], item["start_line"], item["name"])):
        key = (row["path"], row["start_line"], row["end_line"], row["name"])
        if key in seen or any(span["start_line"] <= row["start_line"]
                              and row["end_line"] <= span["end_line"]
                              for span in files[row["path"]]["spans"]):
            continue
        seen.add(key)
        rows.append(row)
    if not rows:
        return excerpt
    navigation = {"kind": SUPPORT_NAVIGATION_KIND, "candidate_count": len(rows), "locators": []}
    result = {**excerpt, "support_navigation": navigation}
    for row in rows:
        selected = navigation["locators"]
        if len(selected) >= MAX_SUPPORT_LOCATORS:
            break
        selected.append(row)
        if (len(json.dumps(navigation, sort_keys=True, ensure_ascii=True).encode())
                > MAX_SUPPORT_LOCATOR_BYTES
                or len(json.dumps(result, sort_keys=True, ensure_ascii=True).encode())
                > MAX_ENVELOPE_BYTES):
            selected.pop()
    return result if navigation["locators"] else excerpt


def extract_edits(workspace: Path, contract: dict, locators: list, *,
                  per_target_budget: bool = False, support_navigation: bool = False,
                  envelope_bounded: bool = False) -> dict:
    """One bounded preimage, not one copy per symbol.

    Prefer complete definitions plus support; on overflow retain whole target
    definitions only where they fit. Multi-target preparation may share a larger
    line ceiling. Internal envelope-bounded allocation shares the existing final
    envelope instead of imposing the legacy raw-source cap as well. No dependency
    outside granted files is opened. Optional same-file candidate locations do not
    evict source or claim dependency resolution. Defaults preserve prepared envelopes.
    """
    if (type(per_target_budget) is not bool or type(support_navigation) is not bool
            or type(envelope_bounded) is not bool):
        raise caller.EmbeddedNokiyError(CODE, "excerpt option selection must be boolean")
    ordered = _edit_locators(contract, locators)
    target_count = len({(row["path"], row["line"]) for row in ordered})
    budget_fields = ({"line_budget": MAX_EXCERPT_LINES * target_count}
                     if per_target_budget and target_count > 1 else {})
    if envelope_bounded:
        budget_fields.update(allocation_mode=_ENVELOPE_ALLOCATION_MODE,
                             byte_budget=MAX_ENVELOPE_BYTES)
    line_budget = budget_fields.get("line_budget", MAX_EXCERPT_LINES)
    byte_budget = budget_fields.get("byte_budget", MAX_EXCERPT_BYTES)
    files = []
    sources = {}
    targets = {}
    support_candidates = []
    line_count = byte_count = 0
    for name in sorted({row["path"] for row in ordered}):
        data = _read_exact(workspace, name)
        try:
            source = data.decode("utf-8")
            tree = ast.parse(source)
        except (UnicodeError, SyntaxError) as error:
            raise caller.EmbeddedNokiyError(CODE, "source cannot be parsed as Python") from error
        definitions = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions.setdefault(node.lineno, []).append(node)
        ranges = []
        for row in ordered:
            if row["path"] != name:
                continue
            nodes = definitions.get(row["line"], [])
            if len(nodes) != 1 or nodes[0].end_lineno is None:
                raise caller.EmbeddedNokiyError(CODE, "DCF line does not identify one complete definition")
            node = nodes[0]
            if support_navigation:
                support_candidates.extend(_direct_support_locators(tree, node, name))
            targets[(name, row["line"])] = (
                min([node.lineno] + [item.lineno for item in node.decorator_list]),
                node.end_lineno,
            )
            ranges.append((node.lineno, node.end_lineno))
            ranges.extend(_supporting_ranges(tree, node))
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        # AST line numbers count CR/LF physical lines, not Unicode separators
        # inside strings. Preserve original line endings and exact source bytes.
        lines = io.StringIO(source, newline="").readlines()
        sources[name] = lines
        spans = []
        for start, end in merged:
            text = "".join(lines[start - 1:end])
            line_count += end - start + 1
            byte_count += len(text.encode("utf-8"))
            spans.append({"start_line": start, "end_line": end, "text": text})
        files.append({"path": name, "source_sha256": hashlib.sha256(data).hexdigest(), "spans": spans})
    # Validate every file/locator before deciding capacity: an oversized first file
    # must not mask a bad locator, symlink or invalid Python in a later file.
    result = {"kind": "edit_preimage", "notice": EDIT_NOTICE, "locators": ordered,
              "files": files, **budget_fields}
    envelope_size = len(json.dumps(result, sort_keys=True, ensure_ascii=True).encode("utf-8"))
    if (line_count <= line_budget and byte_count <= byte_budget
            and all(end - start + 1 <= MAX_EXCERPT_LINES for start, end in targets.values())
            and envelope_size <= MAX_ENVELOPE_BYTES):
        return _add_support_navigation(result, support_candidates) if support_navigation else result

    # All grants, files, parses and target lines have been validated above.
    # Greedily retain complete definitions in canonical locator order; merge
    # overlapping/nested definitions before counting either source budget.
    selected = []
    ranges_by_path = {row["path"]: [] for row in files}
    def partial_files(ranges):
        output = []
        for row in files:
            merged = []
            for start, end in sorted(ranges[row["path"]]):
                if merged and start <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            output.append({"path": row["path"], "source_sha256": row["source_sha256"],
                           "spans": [{"start_line": start, "end_line": end,
                                      "text": "".join(sources[row["path"]][start - 1:end])}
                                     for start, end in merged]})
        return output

    for locator in ordered:
        key = (locator["path"], locator["line"])
        start, end = targets[key]
        if end - start + 1 > MAX_EXCERPT_LINES:
            continue
        ranges_by_path[locator["path"]].append(targets[key])
        candidate_files = partial_files(ranges_by_path)
        candidate_selected = selected + [locator]
        candidate = {"kind": "edit_preimage", "notice": PARTIAL_EDIT_NOTICE,
                     "locators": ordered, "files": candidate_files,
                     "deferred_locators": [row for row in ordered if row not in candidate_selected],
                     **budget_fields}
        spans = [span for row in candidate_files for span in row["spans"]]
        if (sum(span["end_line"] - span["start_line"] + 1 for span in spans) <= line_budget
                and sum(len(span["text"].encode("utf-8")) for span in spans) <= byte_budget
                and len(json.dumps(candidate, sort_keys=True, ensure_ascii=True).encode("utf-8"))
                <= MAX_ENVELOPE_BYTES):
            selected = candidate_selected
            result = candidate
        else:
            ranges_by_path[locator["path"]].pop()
    if selected:
        return _add_support_navigation(result, support_candidates) if support_navigation else result
    reason = ("serialized edit context" if envelope_size > MAX_ENVELOPE_BYTES
              else "combined edit context")
    raise ExcerptBudgetExceeded(CODE, reason + " exceeds budget; use ordinary source_read")


def still_current(workspace: Path, excerpt: dict) -> bool:
    files = excerpt["files"] if excerpt.get("kind") == "edit_preimage" else [excerpt]
    return all(hashlib.sha256(_read_exact(workspace, row["path"])).hexdigest() == row["source_sha256"]
               for row in files)


def _verify_edit_excerpt(workspace: Path, contract: dict, excerpt: dict, *,
                         after_execution: bool = False) -> dict | None:
    partial = "deferred_locators" in excerpt
    per_target_budget = "line_budget" in excerpt
    support_navigation = "support_navigation" in excerpt
    envelope_bounded = "allocation_mode" in excerpt
    fields = {"kind", "notice", "locators", "files"}
    if partial:
        fields.add("deferred_locators")
    if per_target_budget:
        fields.add("line_budget")
    if support_navigation:
        fields.add("support_navigation")
    if envelope_bounded:
        fields.update(("allocation_mode", "byte_budget"))
    if (set(excerpt) != fields
            or excerpt["notice"] != (PARTIAL_EDIT_NOTICE if partial else EDIT_NOTICE)):
        raise caller.EmbeddedNokiyError(CODE, "edit excerpt fields are invalid")
    byte_budget = MAX_EXCERPT_BYTES
    if envelope_bounded:
        if (excerpt["allocation_mode"] != _ENVELOPE_ALLOCATION_MODE
                or type(excerpt["byte_budget"]) is not int
                or excerpt["byte_budget"] != MAX_ENVELOPE_BYTES):
            raise caller.EmbeddedNokiyError(CODE, "edit allocation mode and byte budget are invalid")
        byte_budget = excerpt["byte_budget"]
    locators = _edit_locators(contract, excerpt["locators"])
    line_budget = MAX_EXCERPT_LINES
    if per_target_budget:
        target_count = len({(row["path"], row["line"]) for row in locators})
        if (target_count <= 1 or type(excerpt["line_budget"]) is not int
                or excerpt["line_budget"] != MAX_EXCERPT_LINES * target_count):
            raise caller.EmbeddedNokiyError(CODE, "edit line budget is not bound to distinct targets")
        line_budget = excerpt["line_budget"]
    if partial:
        deferred = excerpt["deferred_locators"]
        if (not isinstance(deferred, list) or len(deferred) >= len(locators)
                or any(not isinstance(row, dict) for row in deferred)
                or deferred != [row for row in locators if row in deferred]):
            raise caller.EmbeddedNokiyError(CODE, "deferred edit locators are invalid")
    files = excerpt["files"]
    if (not isinstance(files, list) or not 1 <= len(files) <= 4
            or any(not isinstance(row, dict) or set(row) != {"path", "source_sha256", "spans"}
                   or not isinstance(row["path"], str)
                   or not isinstance(row["source_sha256"], str)
                   or len(row["source_sha256"]) != 64
                   or any(char not in "0123456789abcdef" for char in row["source_sha256"])
                   or not isinstance(row["spans"], list) or (not partial and not row["spans"])
                   for row in files)
            or [row["path"] for row in files] != sorted({row["path"] for row in locators})):
        raise caller.EmbeddedNokiyError(CODE, "edit excerpt file fields are invalid")
    line_count = byte_count = 0
    for row in files:
        previous_end = 0
        for span in row["spans"]:
            if (not isinstance(span, dict) or set(span) != {"start_line", "end_line", "text"}
                    or type(span["start_line"]) is not int or type(span["end_line"]) is not int
                    or not previous_end < span["start_line"] <= span["end_line"]
                    or not isinstance(span["text"], str)):
                raise caller.EmbeddedNokiyError(CODE, "edit excerpt spans are invalid or overlapping")
            previous_end = span["end_line"]
            line_count += span["end_line"] - span["start_line"] + 1
            byte_count += len(span["text"].encode("utf-8"))
    if line_count > line_budget or byte_count > byte_budget:
        raise caller.EmbeddedNokiyError(CODE, "edit excerpt exceeds combined source budget")
    if support_navigation:
        navigation = excerpt["support_navigation"]
        if (not isinstance(navigation, dict)
                or set(navigation) != {"kind", "candidate_count", "locators"}
                or navigation["kind"] != SUPPORT_NAVIGATION_KIND
                or type(navigation["candidate_count"]) is not int
                or not isinstance(navigation["locators"], list)
                or not 1 <= len(navigation["locators"]) <= MAX_SUPPORT_LOCATORS
                or navigation["candidate_count"] < len(navigation["locators"])
                or len(json.dumps(navigation, sort_keys=True, ensure_ascii=True).encode())
                > MAX_SUPPORT_LOCATOR_BYTES):
            raise caller.EmbeddedNokiyError(CODE, "support navigation is invalid or unbounded")
        names = {row["path"] for row in files}
        for row in navigation["locators"]:
            if (not isinstance(row, dict)
                    or set(row) != {"path", "name", "kind", "start_line", "end_line"}
                    or not isinstance(row["path"], str) or row["path"] not in names
                    or not isinstance(row["name"], str)
                    or not all(part.isidentifier() for part in row["name"].split("."))
                    or row["kind"] not in ("class", "function")
                    or type(row["start_line"]) is not int or type(row["end_line"]) is not int
                    or not 1 <= row["start_line"] <= row["end_line"]):
                raise caller.EmbeddedNokiyError(CODE, "support locator is not an exact same-file candidate")
    if after_execution:
        # The authenticated preimage was checked at preflight. These exact files
        # are writable: capture new identities, not an impossible equality check.
        readback = []
        for row in files:
            digest = hashlib.sha256(_read_exact(workspace, row["path"])).hexdigest()
            readback.append({"path": row["path"], "preimage_sha256": row["source_sha256"],
                             "postimage_sha256": digest, "changed": digest != row["source_sha256"]})
        return {"kind": "edit_postimage", "files": readback, "mission_acceptance": "parent_owned"}
    # Re-extraction checks all hashes and spans in one read per file. Never
    # reinterpret a changed preimage as fresh post-edit verification.
    try:
        current = extract_edits(workspace, contract, locators, per_target_budget=per_target_budget,
                                support_navigation=support_navigation,
                                envelope_bounded=envelope_bounded)
    except ExcerptBudgetExceeded as error:
        raise caller.EmbeddedNokiyError(CODE, "edit excerpt no longer fits its budget") from error
    if current != excerpt:
        raise caller.EmbeddedNokiyError(CODE, "edit preimage changed or was tampered; fresh source_read and context required")


def verify_context_excerpt(workspace: Path, context: dict, contract: dict, *,
                           after_execution: bool = False) -> dict | None:
    summary = context.get("context_summary")
    if not isinstance(summary, str) or CONTEXT_MARKER not in summary:
        return
    if summary.count(CONTEXT_MARKER) != 1:
        raise caller.EmbeddedNokiyError(CODE, "source excerpt marker is ambiguous")
    raw = summary.split(CONTEXT_MARKER, 1)[1]
    if len(raw.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise caller.EmbeddedNokiyError(CODE, "source excerpt context exceeds its budget")
    try:
        excerpt = json.loads(raw, object_pairs_hook=caller._unique_object,
                             parse_constant=caller._invalid_constant)
    except ValueError as error:
        raise caller.EmbeddedNokiyError(CODE, "source excerpt context is invalid") from error
    if isinstance(excerpt, dict) and excerpt.get("kind") == "edit_preimage":
        return _verify_edit_excerpt(workspace, contract, excerpt, after_execution=after_execution)
    fields = {"path", "start_line", "end_line", "source_sha256", "text"}
    if (not isinstance(excerpt, dict)
            or set(excerpt) not in (fields, fields | {"supporting_spans"})
            or not isinstance(excerpt["path"], str)
            or type(excerpt["start_line"]) is not int
            or type(excerpt["end_line"]) is not int
            or not isinstance(excerpt["source_sha256"], str)
            or not isinstance(excerpt["text"], str)):
        raise caller.EmbeddedNokiyError(CODE, "source excerpt fields are invalid")
    include_dependencies = "supporting_spans" in excerpt
    if include_dependencies:
        spans = excerpt["supporting_spans"]
        if (not isinstance(spans, list) or any(
                not isinstance(span, dict)
                or set(span) != {"start_line", "end_line", "text"}
                or type(span["start_line"]) is not int
                or type(span["end_line"]) is not int
                or not 1 <= span["start_line"] <= span["end_line"]
                or not isinstance(span["text"], str) for span in spans)):
            raise caller.EmbeddedNokiyError(CODE, "supporting span fields are invalid")
    locator = {"path": excerpt["path"], "line": excerpt["start_line"],
               "generation_id": contract.get("dcf_generation", {}).get("generation_id")}
    try:
        current = extract(workspace, contract, locator, include_dependencies=include_dependencies)
    except ExcerptBudgetExceeded as error:
        # Verification is an integrity check, never a caller capacity fallback.
        raise caller.EmbeddedNokiyError(CODE, "source excerpt no longer fits its budget") from error
    if current != excerpt:
        raise caller.EmbeddedNokiyError(CODE, "source excerpt no longer matches the admitted definition")
