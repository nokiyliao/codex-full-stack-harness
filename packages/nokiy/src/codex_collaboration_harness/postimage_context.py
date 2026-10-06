"""Bounded, context-only postimage projection for admitted verifier results.

Definition wire v1 (optional ``source_postimages`` on a raw verifier observation)::

    {schema_version, kind, jspace_semantic_sha256, notice, files: [
        {path, preimage_sha256, postimage_sha256, locators: [
            {qualified_name, kind, line, start_line, end_line, complete: true}
        ], spans: [{start_line, end_line, text}]}
    ]}

For admitted non-Python text, or on explicit Python definition projection size
overflow, the same field can hold (all projected files use the delta envelope)::

    {schema_version: nokiy_source_postimage_delta_v1, kind: verifier_postimage_delta,
     jspace_semantic_sha256, notice, files: [
        {path, preimage_sha256, postimage_sha256, coverage: all_text_changes, hunks: [
            {before_start_line, before_line_count, after_start_line, after_line_count,
             before_text, after_text}
        ]}
    ]}

Delta v2 adds ``locators`` to each delta file, with qualified_name, kind, line,
start_line and end_line only: navigation, never a completeness claim.

Paths are exact workspace-relative .py/.js/.mjs/.cjs/.jsx/.ts/.tsx/.rs source paths.
Non-Python text uses delta v1 only, without AST or definition completeness claims.
Preimage/postimage SHAs identify
exact source bytes; jspace_semantic_sha256 echoes the captured authorization
binding. Digests are 64-hex; lines are one-based inclusive physical lines,
preserving newlines.
Locator kinds are function, async_function, class or binding. Only definition locators
claim completeness; sorted nonoverlapping spans also contain deduplicated
same-file syntactic support. Delta hunks instead cover every textual change with
up to three context lines at each boundary, not complete definitions, dependencies
or files.
Hunk starts are one-based offsets (including empty sides at line count + 1);
counts may be zero. Both sides count against the same total line budget.
Omission is ordinary-read fallback, not failure of the verifier. Neither envelope
is verifier facts, authority or acceptance.
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
import difflib
import hashlib
import io
import json
from pathlib import Path

from . import source_excerpt


SCHEMA_VERSION = "nokiy_source_postimages_v1"
DELTA_SCHEMA_VERSION = "nokiy_source_postimage_delta_v1"
DELTA_NAVIGATION_SCHEMA_VERSION = "nokiy_source_postimage_delta_v2"
SOURCE_SUFFIXES = frozenset({".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".rs"})
MAX_DELTA_LOCATORS = 64
MAX_DELTA_NAME_BYTES = 512
MAX_FILES = 4
MAX_LINES = 600
MAX_JSON_BYTES = 32_768
# Per changed file: actual middle-region scans, candidate visits (including
# out-of-range repeats) and extension comparisons, across all matching regions.
MAX_MATCH_WORK = 4_000_000
# Bound private preimage capture too; exceeding either limit disables context,
# rather than selecting a prefix of the writable sources.
MAX_CAPTURE_FILES = 32
MAX_CAPTURE_BYTES = 4 * source_excerpt.MAX_SOURCE_BYTES
NOTICE = (
    "Only listed definitions/bindings are complete. Same-file syntactic support "
    "is not dependency resolution or complete-file proof. Context only: semantically "
    "review against the task; not verifier facts, acceptance or a new grant. "
    "Missing evidence requires ordinary granted source_read."
)
DELTA_NOTICE = (
    "All textual changes between each pinned preimage and fresh postimage; hunks "
    "include up to three surrounding physical context lines at each boundary, "
    "not complete definitions, dependencies or files. "
    "after_text is postimage source at after_start_line; before_text is old source. "
    "For each listed path, previously supplied excerpts matching preimage_sha256 "
    "can be combined with these hunks for postimage_sha256: text outside hunks is "
    "unchanged, but line offsets may shift. Unseen surrounding semantics are not "
    "supplied. Context only: semantically "
    "review against the task; not verifier facts, acceptance or a new grant. "
    "Missing or newer evidence requires ordinary granted source_read."
)
DELTA_NAVIGATION_NOTICE = DELTA_NOTICE + (
    " AST locators identify qualified units in the pinned postimage; "
    "they do not supply complete source text or prove dependencies."
)


@dataclass(frozen=True)
class Preimages:
    workspace: Path
    binding: str
    pins: dict[str, str]
    sources: dict[str, tuple[str, ast.Module | None]]
    dcf_generation: dict | None = None


@dataclass(frozen=True)
class _Unit:
    key: tuple
    names: tuple[str, ...]
    kind: str
    node: ast.AST
    start: int
    end: int
    parents: tuple[_Unit, ...]


def _sha(value) -> str | None:
    if (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdefABCDEF" for char in value)):
        return value.lower()
    return None


def _safe_path(name) -> bool:
    if (not source_excerpt._exact_source_path(name) or Path(name).suffix not in SOURCE_SUFFIXES
            or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        return False
    try:
        name.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _pins(workspace: Path, contract: dict, *, dcf_generation: dict | None = None,
          observed_pins: dict[str, str] | None = None) -> dict[str, str | None] | None:
    """Exact grants plus snapshot pins or caller-validated DCF capture identity."""
    operations = contract.get("allowed_operations")
    denied = contract.get("denied_operations", [])
    if (contract.get("source_read") is not True
            or not isinstance(operations, list)
            or not all(isinstance(op, str) and op for op in operations)
            or not {"command", "read", "modify"} <= set(operations)
            or not isinstance(denied, list)
            or not all(isinstance(op, str) and op for op in denied)
            or any(op in denied for op in ("command", "read", "modify"))
            or _sha(contract.get("authorization_semantic_sha256")) is None
            or not workspace.is_absolute() or workspace.resolve(strict=True) != workspace
            or contract.get("repo_root") != str(workspace)):
        return None
    reads, writes = contract.get("read_scopes"), contract.get("write_scopes")
    if (not isinstance(reads, list) or not isinstance(writes, list)
            or not all(isinstance(name, str) for name in reads + writes)):
        return None
    normalized_writes = set()
    for name in writes:
        if name.startswith("/"):
            path = Path(name)
            if path.as_posix() != name or ".." in path.parts or "." in name.split("/"):
                return None
            try:
                name = path.relative_to(workspace).as_posix()
            except ValueError:
                continue
        if _safe_path(name):
            normalized_writes.add(name)
    names = sorted({name for name in reads if _safe_path(name)} & normalized_writes)
    generation = contract.get("dcf_generation")
    if not names or not isinstance(generation, dict) or len(names) > MAX_CAPTURE_FILES:
        return None
    if dcf_generation is not None and generation != dcf_generation:
        return None
    # Only absence permits observed-byte identity. A present malformed/stale
    # snapshot remains mandatory, including on an otherwise validated DCF grant.
    if "source_snapshot" not in generation:
        if (dcf_generation is None
                or contract.get("schema_version") != "jspace_contract_v2"):
            return None
        return {name: observed_pins.get(name) if observed_pins is not None else None
                for name in names}
    snapshot = generation["source_snapshot"]
    if not isinstance(snapshot, dict):
        return None
    pins = {}
    for name in names:
        row = snapshot.get(name)
        digest = _sha(row.get("sha256")) if isinstance(row, dict) else None
        if digest is None:
            return None
        pins[name] = digest
    return pins


def _identity_rows(pins: dict[str, str]) -> list[dict]:
    return [{"path": name, "source_sha256": digest} for name, digest in pins.items()]


def _read_source(workspace: Path, name: str) -> bytes:
    # Retain the Python reader's existing call/validation behavior. The broader
    # reader is used only for paths selected by our exact source grant filter.
    if Path(name).suffix == ".py":
        return source_excerpt._read_exact(workspace, name)
    return source_excerpt._read_exact(workspace, name, python_only=False)


def _decode_source(name: str, data: bytes) -> str:
    source = data.decode("utf-8")
    if Path(name).suffix != ".py" and any(
            (ord(char) < 32 and char not in "\t\n\r\v\f") or 127 <= ord(char) <= 159
            for char in source):
        raise ValueError("non-Python source contains binary control characters")
    return source


def _still_current(workspace: Path, pins: dict[str, str]) -> bool:
    # source_excerpt.still_current is deliberately Python-only; do not relax it.
    python_pins = {name: digest for name, digest in pins.items() if Path(name).suffix == ".py"}
    if python_pins and not source_excerpt.still_current(workspace, {
            "kind": "edit_preimage", "files": _identity_rows(python_pins)}):
        return False
    return all(hashlib.sha256(_read_source(workspace, name)).hexdigest() == digest
               for name, digest in pins.items() if Path(name).suffix != ".py")


def capture_preimages(workspace: Path, contract: dict, *,
                      validated_dcf_generation: dict | None = None) -> Preimages | None:
    """Capture before edits; the optional generation must come from caller validation.

    It is an internal fact, never a worker/contract opt-in or a synthetic snapshot.
    Unavailability must not break verification.
    """
    try:
        dcf_generation = None
        if (isinstance(validated_dcf_generation, dict)
                and contract.get("dcf_generation") == validated_dcf_generation
                and contract.get("schema_version") == "jspace_contract_v2"
                and validated_dcf_generation.get("context_mode") != "local_workspace_jspace"
                and isinstance(validated_dcf_generation.get("action_freshness"), dict)
                and isinstance(validated_dcf_generation.get("generation_id"), str)
                and validated_dcf_generation["generation_id"]
                and "source_snapshot" not in validated_dcf_generation):
            # Retain generation identity independently of mutable caller inputs.
            dcf_generation = copy.deepcopy(validated_dcf_generation)
        expected = _pins(workspace, contract, dcf_generation=dcf_generation)
        if expected is None:
            return None
        binding = contract["authorization_semantic_sha256"]
        pins, sources = {}, {}
        size = 0
        for name, expected_digest in expected.items():
            data = _read_source(workspace, name)
            size += len(data)
            digest = hashlib.sha256(data).hexdigest()
            if (size > MAX_CAPTURE_BYTES
                    or (expected_digest is not None and digest != expected_digest)):
                return None
            source = _decode_source(name, data)
            tree = None
            if Path(name).suffix == ".py":
                tree = ast.parse(source)
                _units(tree)  # Ambiguous qualified bindings cannot become complete targets.
            pins[name] = digest
            sources[name] = (source, tree)
        if (contract.get("authorization_semantic_sha256") != binding
                or pins != _pins(workspace, contract, dcf_generation=dcf_generation, observed_pins=pins)
                or not _still_current(workspace, pins)
                or pins != _pins(workspace, contract, dcf_generation=dcf_generation, observed_pins=pins)
                or contract.get("authorization_semantic_sha256") != binding):
            return None
        return Preimages(workspace, binding, pins, sources, dcf_generation)
    except Exception:
        return None


def known_success(observation: dict) -> bool:
    return (isinstance(observation, dict)
            and observation.get("success") is True
            and type(observation.get("exit_code")) is int and observation["exit_code"] == 0
            and observation.get("outcome") == "known"
            and observation.get("process_reaped") is True
            and observation.get("process_group_empty") is True)


def _start(node: ast.AST) -> int:
    return min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", ())])


def _units(tree: ast.Module) -> dict[tuple, _Unit]:
    units = {}
    definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def visit(node, scope=(), parents=(), scope_kind="module"):
        names, kind, key = (), None, None
        if isinstance(node, definitions):
            names = (".".join(scope + (node.name,)),)
            kind = ("class" if isinstance(node, ast.ClassDef) else
                    "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
            key = ("definition", names)
        elif scope_kind != "function":
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = tuple(sorted({".".join(scope + (part.id,))
                                      for target in targets for part in ast.walk(target)
                                      if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store)}))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = tuple(sorted({".".join(scope + (alias.asname or (
                    alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name),))
                    for alias in node.names if alias.name != "*"}))
            if names:
                kind, key = "binding", ("binding", names)
        if key is not None:
            start, end = _start(node), node.end_lineno
            if key in units or type(end) is not int or not 1 <= start <= end:
                raise ValueError("ambiguous or incomplete qualified source unit")
            unit = _Unit(key, names, kind, node, start, end, parents)
            units[key] = unit
            if isinstance(node, definitions):
                scope += (node.name,)
                parents += (unit,)
                scope_kind = "class" if isinstance(node, ast.ClassDef) else "function"
        for child in ast.iter_child_nodes(node):
            visit(child, scope, parents, scope_kind)

    visit(tree)
    return units


def _lines(source: str) -> list[str]:
    # Universal physical newlines without translating their exact source text.
    return io.StringIO(source, newline="").readlines()


class _BoundedMatcher(difflib.SequenceMatcher):
    """Exact no-junk matching with one deterministic budget across all regions."""

    def __init__(self, *, a: list[str], b: list[str]):
        self._remaining = MAX_MATCH_WORK
        super().__init__(a=a, b=b, autojunk=False)

    def get_matching_blocks(self):
        # The first full-middle scan necessarily visits every indexed equal
        # pair. Reject an impossible budget in linear time using that index.
        if self.matching_blocks is None:
            work = len(self.a) + sum(
                len(self.b2j.get(line, ())) for line in self.a)
            if work > self._remaining:
                raise ValueError("postimage matching work budget exceeded")
        return super().get_matching_blocks()

    def _spend(self):
        self._remaining -= 1
        if self._remaining < 0:
            raise ValueError("postimage matching work budget exceeded")

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        # SequenceMatcher's no-junk recurrence and tie order. Count every scan
        # and candidate, not just in-range/equal pairs: subdivision can revisit
        # the same index lists. Junk/popularity filtering is never enabled.
        a, b, b2j = self.a, self.b, self.b2j
        ahi = len(a) if ahi is None else ahi
        bhi = len(b) if bhi is None else bhi
        besti, bestj, bestsize = alo, blo, 0
        j2len = {}
        for i in range(alo, ahi):
            self._spend()
            previous, j2len = j2len, {}
            for j in b2j.get(a[i], ()):
                self._spend()
                if j < blo:
                    continue
                if j >= bhi:
                    break
                size = j2len[j] = previous.get(j - 1, 0) + 1
                if size > bestsize:
                    besti, bestj, bestsize = i - size + 1, j - size + 1, size
        while besti > alo and bestj > blo:
            self._spend()
            if a[besti - 1] != b[bestj - 1]:
                break
            besti, bestj, bestsize = besti - 1, bestj - 1, bestsize + 1
        while besti + bestsize < ahi and bestj + bestsize < bhi:
            self._spend()
            if a[besti + bestsize] != b[bestj + bestsize]:
                break
            bestsize += 1
        return difflib.Match(besti, bestj, bestsize)


@dataclass(frozen=True)
class _LineChanges:
    old_lines: list[str]
    new_lines: list[str]
    opcodes: list[tuple[str, int, int, int, int]]

    def grouped_opcodes(self, n: int = 3):
        """Standard opcode grouping, without rematching or mutating the cache."""
        codes = self.opcodes.copy()
        if not codes:
            return
        if codes[0][0] == "equal":
            tag, i1, i2, j1, j2 = codes[0]
            codes[0] = (tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2)
        if codes[-1][0] == "equal":
            tag, i1, i2, j1, j2 = codes[-1]
            codes[-1] = (tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n))
        group = []
        for tag, i1, i2, j1, j2 in codes:
            if tag == "equal" and i2 - i1 > 2 * n:
                group.append((tag, i1, i1 + n, j1, j1 + n))
                yield group
                group = []
                i1, j1 = i2 - n, j2 - n
            group.append((tag, i1, i2, j1, j2))
        if group and not (len(group) == 1 and group[0][0] == "equal"):
            yield group


def _line_changes(before: str, source: str) -> _LineChanges:
    """Linear exact boundary trimming, then bounded matching of only the middle."""
    old_lines, new_lines = _lines(before), _lines(source)
    old_end, new_end = len(old_lines), len(new_lines)
    prefix = 0
    while prefix < min(old_end, new_end) and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    while (old_end > prefix and new_end > prefix
           and old_lines[old_end - 1] == new_lines[new_end - 1]):
        old_end, new_end = old_end - 1, new_end - 1
    opcodes = [("equal", 0, prefix, 0, prefix)] if prefix else []
    if old_end > prefix and new_end > prefix:
        matcher = _BoundedMatcher(a=old_lines[prefix:old_end], b=new_lines[prefix:new_end])
        opcodes.extend((tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix)
                       for tag, i1, i2, j1, j2 in matcher.get_opcodes())
    elif old_end > prefix:
        opcodes.append(("delete", prefix, old_end, prefix, prefix))
    elif new_end > prefix:
        opcodes.append(("insert", prefix, prefix, prefix, new_end))
    if old_end < len(old_lines):
        opcodes.append(("equal", old_end, len(old_lines), new_end, len(new_lines)))
    return _LineChanges(old_lines, new_lines, opcodes)


def _owner(units: dict, line: int) -> _Unit:
    candidates = [unit for unit in units.values() if unit.start <= line <= unit.end]
    if not candidates:
        raise ValueError("change outside a qualified definition or binding")
    unit = min(candidates, key=lambda unit: (unit.end - unit.start, -len(unit.parents)))
    # A one-line class header/body must be reviewed as a whole class. Unrelated
    # statements sharing a physical line cannot be attributed by a line diff.
    for parent in unit.parents:
        if parent.node.lineno == line:
            unit = parent
            break
    ancestors = {parent.key for parent in unit.parents}
    if any(candidate.key != unit.key and candidate.key not in ancestors
           and unit.key not in {parent.key for parent in candidate.parents}
           for candidate in candidates):
        raise ValueError("ambiguous changed physical line")
    return unit


def _class_separator(unit: _Unit, line: int, text: str) -> bool:
    """Ignore only blank gaps between class statements, never literal/header lines."""
    return (unit.kind == "class" and not text.strip()
            and line >= _start(unit.node.body[0])
            and not any(_start(node) <= line <= node.end_lineno for node in unit.node.body))


def _changed_units(before: tuple[str, ast.Module], changes: _LineChanges, tree: ast.Module, *,
                   units: dict | None = None) -> list[_Unit]:
    old, new = _units(before[1]), _units(tree) if units is None else units
    selected = {}
    old_lines, new_lines = changes.old_lines, changes.new_lines
    old_blocks = [(node.lineno, node.end_lineno) for node in before[1].body]
    new_blocks = [(node.lineno, node.end_lineno) for node in tree.body]
    for tag, i1, i2, j1, j2 in changes.opcodes:
        if tag == "equal":
            continue
        for line in range(j1 + 1, j2 + 1):
            if (not new_lines[line - 1].strip()
                    and not any(start <= line <= end for start, end in new_blocks)):
                continue  # Empty separators do not require a source-unit owner.
            unit = _owner(new, line)
            if _class_separator(unit, line, new_lines[line - 1]):
                continue
            selected[unit.key] = unit
        for line in range(i1 + 1, i2 + 1):
            if (not old_lines[line - 1].strip()
                    and not any(start <= line <= end for start, end in old_blocks)):
                continue
            unit = _owner(old, line)
            if _class_separator(unit, line, old_lines[line - 1]):
                continue
            # A removed nested definition needs its complete surviving parent.
            # A top-level deletion has no complete postimage proof: ordinary read.
            while unit.key not in new:
                if not unit.parents:
                    raise ValueError("removed unit has no complete postimage owner")
                unit = unit.parents[-1]
            selected[unit.key] = new[unit.key]
    return sorted((unit for unit in selected.values()
                   if not any(parent.key in selected for parent in unit.parents)),
                  key=lambda unit: (unit.start, unit.end, unit.names))


def _merge(ranges: list[tuple[int, int]], line_count: int) -> list[tuple[int, int]]:
    merged = []
    for start, end in sorted(ranges):
        if not 1 <= start <= end <= line_count:
            raise ValueError("source locator is not complete")
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _supporting_ranges(tree: ast.Module, unit: _Unit) -> list[tuple[int, int]]:
    """Complete lexical functions; class headers, statements and referenced siblings.

    Name/attribute spellings conservatively select complete sibling definitions,
    not receiver resolution or dependency proof. Unrelated class definitions do
    not supply a method's lexical scope. All selected support still has to fit.
    """
    ranges, nodes, classes, siblings = [], [unit.node], set(), {}
    for parent in unit.parents:
        if parent.kind != "class":
            ranges.append((parent.start, parent.end))
            nodes.append(parent.node)
            continue
        node = parent.node
        classes.add(node)
        ranges.append((parent.start, _start(node.body[0]) - 1))
        nodes.extend(node.decorator_list + node.bases + node.keywords)
        nodes.extend(getattr(node, "type_params", ()))
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                siblings.setdefault(statement.name, []).append(statement)
            else:
                ranges.append((_start(statement), statement.end_lineno))
                nodes.append(statement)

    pending, seen, followed = list(nodes), set(nodes) | classes, set()
    while pending:
        references = set()
        for part in ast.walk(pending.pop()):
            if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load):
                references.add(part.id)
            elif isinstance(part, ast.Attribute):
                references.add(part.attr)
        for name in sorted(references - followed):
            followed.add(name)
            for support in siblings.get(name, ()):
                if support in seen:
                    continue
                seen.add(support)
                ranges.append((_start(support), support.end_lineno))
                nodes.append(support)
                pending.append(support)

    # Reuse the module support walker with only the selected class context.
    # Self-references (including from module helpers) must not reselect a whole
    # enclosing class. Other referenced module definitions remain complete.
    module = ast.Module(body=[node for node in tree.body if node not in classes], type_ignores=[])
    target = ast.Module(body=nodes, type_ignores=[])
    target.decorator_list = []
    target.lineno, target.end_lineno = unit.node.lineno, unit.end
    ranges.extend(source_excerpt._supporting_ranges(module, target))
    return ranges


def _project_file(name: str, digest: str, preimage: str, before, changes: _LineChanges, tree: ast.Module, *,
                  navigation: dict | None = None) -> dict:
    units = _units(tree)
    targets = _changed_units(before, changes, tree, units=units)
    if not targets:
        raise ValueError("no complete changed source units")
    ranges, locators, support = [], [], []
    for unit in targets:
        ranges.append((unit.start, unit.end))
        locators.extend({"qualified_name": symbol, "kind": unit.kind,
                         "line": unit.node.lineno, "start_line": unit.start,
                         "end_line": unit.end, "complete": True} for symbol in unit.names)
        support.extend(_supporting_ranges(tree, unit))
    ranges.extend(support)
    lines = changes.new_lines
    spans = [{"start_line": start, "end_line": end, "text": "".join(lines[start - 1:end])}
             for start, end in _merge(ranges, len(lines))]
    if navigation is not None:
        navigation[name] = (targets, support, units)
    return {"path": name, "preimage_sha256": preimage, "postimage_sha256": digest,
            "locators": locators, "spans": spans}


def _project_delta_file(name: str, digest: str, preimage: str, changes: _LineChanges) -> dict:
    """Exact, complete textual changes after source/grant and language eligibility."""
    old_lines, new_lines = changes.old_lines, changes.new_lines
    hunks = []
    for group in changes.grouped_opcodes(3):
        i1, j1 = group[0][1], group[0][3]
        i2, j2 = group[-1][2], group[-1][4]
        hunks.append({"before_start_line": i1 + 1, "before_line_count": i2 - i1,
                      "after_start_line": j1 + 1, "after_line_count": j2 - j1,
                      "before_text": "".join(old_lines[i1:i2]),
                      "after_text": "".join(new_lines[j1:j2])})
    return {"path": name, "preimage_sha256": preimage, "postimage_sha256": digest,
            "coverage": "all_text_changes", "hunks": hunks}


def _with_delta_navigation(envelope: dict, navigation: dict) -> dict:
    """Upgrade a valid v1 delta atomically; reuse definition-mode coordinates."""
    files, count = [], 0
    for row in envelope["files"]:
        if row["path"] not in navigation:
            return envelope
        targets, support, units = navigation[row["path"]]
        parent_keys = {unit.parents[-1].key for unit in targets if unit.parents}
        # Use individual syntactic support ranges, not merged source spans:
        # adjoining partial class context must not synthesize a class locator.
        candidates = targets + [unit for unit in units.values()
                                if unit.kind in ("function", "async_function")
                                and (not unit.parents or unit.parents[-1].key in parent_keys)
                                and any(start <= unit.start and unit.end <= end
                                       for start, end in support)]
        locators = {}
        for unit in candidates:
            line = unit.node.lineno
            if (unit.kind not in ("function", "async_function", "class", "binding")
                    or any(type(value) is not int or value <= 0
                           for value in (unit.start, line, unit.end))
                    or not unit.start <= line <= unit.end):
                return envelope
            for name in unit.names:
                if (not isinstance(name, str) or not all(name.split("."))
                        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)):
                    return envelope
                try:
                    if len(name.encode("utf-8")) > MAX_DELTA_NAME_BYTES:
                        return envelope
                except UnicodeEncodeError:
                    return envelope
                locator = {"qualified_name": name, "kind": unit.kind, "line": line,
                           "start_line": unit.start, "end_line": unit.end}
                if name in locators:
                    if locators[name] != locator:
                        return envelope
                    continue
                count += 1
                if count > MAX_DELTA_LOCATORS:
                    return envelope
                locators[name] = locator
        files.append({**row, "locators": sorted(locators.values(), key=lambda item: (
            item["start_line"], item["end_line"], item["qualified_name"]))})
    candidate = {**envelope, "schema_version": DELTA_NAVIGATION_SCHEMA_VERSION,
                 "notice": DELTA_NAVIGATION_NOTICE, "files": files}
    if len(json.dumps(candidate, sort_keys=True, ensure_ascii=True).encode("ascii")) > MAX_JSON_BYTES:
        return envelope
    return candidate


def project(workspace: Path, contract: dict, preimages: Preimages | None,
            observation: dict, emitted: set[tuple[str, str, str]]) -> dict | None:
    """All-or-none context; never mutate observations or the instance's dedup set."""
    if preimages is None or not known_success(observation):
        return None
    try:
        if (workspace != preimages.workspace
                or contract.get("authorization_semantic_sha256") != preimages.binding
                or _pins(workspace, contract, dcf_generation=preimages.dcf_generation,
                         observed_pins=preimages.pins) != preimages.pins):
            return None
        files, identities, changes_by_path, navigation = [], {}, {}, {}
        line_count = changed_count = size = 0
        requires_delta = False
        for name, preimage in preimages.pins.items():
            data = _read_source(workspace, name)
            size += len(data)
            if size > MAX_CAPTURE_BYTES:
                return None
            digest = hashlib.sha256(data).hexdigest()
            identities[name] = digest
            if digest == preimage:
                continue
            changed_count += 1
            if changed_count > MAX_FILES:
                return None
            if (preimages.binding, name, digest) in emitted:
                continue
            source = _decode_source(name, data)
            is_python = Path(name).suffix == ".py"
            tree = ast.parse(source) if is_python else None
            changes = _line_changes(preimages.sources[name][0], source)
            if is_python:
                row = _project_file(name, digest, preimage, preimages.sources[name], changes, tree,
                                    navigation=navigation)
                line_count += sum(span["end_line"] - span["start_line"] + 1 for span in row["spans"])
            else:
                requires_delta = True
                # Identity only until the coherent all-file delta is built below.
                row = {"path": name, "preimage_sha256": preimage, "postimage_sha256": digest}
            files.append(row)
            changes_by_path[name] = changes
        if not files:
            return None
        envelope = {"schema_version": SCHEMA_VERSION, "kind": "verifier_postimage_context",
                    "jspace_semantic_sha256": preimages.binding, "notice": NOTICE, "files": files}
        # Text sources require all-file v1 deltas. Python must still pass definition
        # projection first; only its size overflow permits Python-only delta mode.
        # Continue through every file first: an error is never fallback.
        if (requires_delta or line_count > MAX_LINES
                or len(json.dumps(envelope, sort_keys=True, ensure_ascii=True).encode("ascii")) > MAX_JSON_BYTES):
            files = [_project_delta_file(row["path"], row["postimage_sha256"], row["preimage_sha256"],
                                         changes_by_path[row["path"]])
                     for row in files]
            line_count = sum(hunk["before_line_count"] + hunk["after_line_count"]
                             for row in files for hunk in row["hunks"])
            envelope = {"schema_version": DELTA_SCHEMA_VERSION, "kind": "verifier_postimage_delta",
                        "jspace_semantic_sha256": preimages.binding, "notice": DELTA_NOTICE, "files": files}
            if (line_count > MAX_LINES
                    or len(json.dumps(envelope, sort_keys=True, ensure_ascii=True).encode("ascii")) > MAX_JSON_BYTES):
                return None
            if not requires_delta:
                envelope = _with_delta_navigation(envelope, navigation)
        if (contract.get("authorization_semantic_sha256") != preimages.binding
                or _pins(workspace, contract, dcf_generation=preimages.dcf_generation,
                         observed_pins=preimages.pins) != preimages.pins
                or not _still_current(workspace, identities)
                or _pins(workspace, contract, dcf_generation=preimages.dcf_generation,
                         observed_pins=preimages.pins) != preimages.pins
                or contract.get("authorization_semantic_sha256") != preimages.binding):
            return None
        return envelope
    except Exception:
        return None
