"""Corpus resolution for native DCF, deliberately separate from file parse cache.

Adapted from registry.c / extract_calls.c in DeusData/codebase-memory-mcp
1f9b4db12c745e1b4554f822667627aa09d4e6bd (MIT; vendor/codebase_memory/LICENSE).
Qualified bindings precede file/scope lookup, then explicitly weak candidates.
No receiver type inference or upstream LSP parity is claimed.
"""

from __future__ import annotations

import builtins
from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def resolve_calls(symbols: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    rows = list(symbols)
    qualified: dict[tuple[str, str], list[Any]] = defaultdict(list)
    names: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in rows:
        language = row.navigation.get("language", row.kind.split("_")[0])
        qualified[language, row.navigation.get("lookup_fqn", row.fqn)].append(row)
        names[language, row.name].append(row)
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        language = row.navigation.get("language", row.kind.split("_")[0])
        resolved = []
        specs = row.calls
        if not specs:
            # Legacy/TS references are navigation hints, never exact call edges.
            specs = [
                {
                    "text": text,
                    "line": row.line,
                    "targets": [],
                    "weak_allowed": text.isidentifier(),
                    "reason": "syntax_reference",
                }
                for text in row.references
            ]
        for spec in specs:
            matches: list[Any] = []
            confidence = "unresolved"
            for target in spec.get("targets", []):
                matches = qualified.get((language, target), [])
                if matches:
                    confidence = "exact" if len(matches) == 1 else "ambiguous"
                    break
            if not matches and spec.get("weak_allowed"):
                name = spec["text"]
                if not (language == "python" and hasattr(builtins, name)):
                    matches = names.get((language, name), [])
                    confidence = "weak" if matches else "unresolved"
            resolved.append(
                {
                    **spec,
                    "confidence": confidence,
                    "candidate_ids": sorted({item.entity_id for item in matches}),
                }
            )
        result[row.entity_id] = resolved
    return result
