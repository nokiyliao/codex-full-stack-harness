"""Optional, source-bound same-file navigation for the Rust source reader."""
import ast
import hashlib
import json

from . import embedded_nokiy as caller, source_excerpt

ENV = "NOKIY_SOURCE_NAVIGATION"
MAX_BYTES = 65536


def build(workspace, contract):
    """Derive hints only from exact admitted reads; never modify a grant."""
    if (not isinstance(contract, dict) or contract.get("source_read") is not True
            or "read" not in contract.get("allowed_operations", [])
            or "read" in contract.get("denied_operations", [])):
        return None
    catalog = {"schema_version": "nokiy_source_navigation_v1",
               "workspace": str(workspace), "files": {}}
    for name in contract.get("read_scopes", []):
        if len(catalog["files"]) >= 32:
            break
        if not source_excerpt._exact_python_path(name):
            continue
        try:
            data = source_excerpt._read_exact(workspace, name)
            tree = ast.parse(data)
        except (caller.EmbeddedNokiyError, SyntaxError, UnicodeError, ValueError, RecursionError):
            continue
        owners = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            first = min([node.lineno] + [item.lineno for item in node.decorator_list])
            rows = source_excerpt._direct_support_locators(tree, node, name)
            definitions = [[r["name"], "c" if r["kind"] == "class" else "f",
                            r["start_line"], r["end_line"]] for r in rows
                           if not first <= r["start_line"] <= r["end_line"] <= node.end_lineno]
            if definitions:
                owners.append({"range": [first, node.end_lineno], "definitions": definitions[:32]})
            if len(owners) >= 512:
                break
        if not owners:
            continue
        catalog["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "owners": owners}
        encoded = json.dumps(catalog, ensure_ascii=True, separators=(",", ":"))
        if len(encoded.encode()) > MAX_BYTES:
            del catalog["files"][name]
    if not catalog["files"]:
        return None
    return json.dumps(catalog, ensure_ascii=True, separators=(",", ":"))
