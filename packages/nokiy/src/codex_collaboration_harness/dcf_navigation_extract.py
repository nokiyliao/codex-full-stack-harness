"""Native Python syntax extraction; never imports or executes indexed code.

Scope/import-first resolution and receiver exemptions adapted from
DeusData/codebase-memory-mcp, commit 1f9b4db12c745e1b4554f822667627aa09d4e6bd
(registry.c / extract_calls.c). See vendor/codebase_memory/LICENSE (MIT).
File caches contain syntax/bindings, not corpus-dependent resolution results.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _body(node: ast.AST) -> list[ast.AST]:
    body = getattr(node, "body", [])
    return body if isinstance(body, list) else [body]


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None  # A call/subscript receiver is NOT its terminal attribute name.


@dataclass
class _Scope:
    node: ast.AST
    lookup: str
    parent: _Scope | None
    bindings: dict[str, tuple[str, str] | None] = field(default_factory=dict)
    writes: set[str] = field(default_factory=set)

    def bind(self, name: str, value: tuple[str, str] | None) -> None:
        if name in self.bindings and self.bindings[name] != value:
            value = None
        self.bindings[name] = value


def _scope(node: ast.AST, lookup: str, parent: _Scope | None, package: str) -> _Scope:
    scope = _Scope(node, lookup, parent)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                scope.bind(arg.arg, None)

    class Binder(ast.NodeVisitor):
        def visit_Name(self, child: ast.Name) -> None:
            if isinstance(child.ctx, (ast.Store, ast.Del)):
                scope.writes.add(child.id)
                scope.bind(child.id, None)

        def visit_Import(self, child: ast.Import) -> None:
            for alias in child.names:
                scope.bind(
                    alias.asname or alias.name.split(".")[0],
                    (alias.name if alias.asname else alias.name.split(".")[0], "import"),
                )

        def visit_ImportFrom(self, child: ast.ImportFrom) -> None:
            parts = package.split(".") if package else []
            if child.level:
                # An invalid relative import must not resolve as an absolute one.
                base = parts[: len(parts) - child.level + 1] if child.level <= len(parts) else []
                module = (
                    ".".join([*base, *([child.module] if child.module else [])]) if base else ""
                )
            else:
                module = child.module or ""
            for alias in child.names:
                if alias.name != "*":
                    scope.bind(
                        alias.asname or alias.name,
                        (f"{module}.{alias.name}", "import") if module else None,
                    )

        def _definition(self, child: Any) -> None:
            scope.bind(
                child.name,
                (
                    f"{lookup}.{child.name}",
                    "class" if isinstance(child, ast.ClassDef) else "definition",
                ),
            )

        visit_FunctionDef = _definition
        visit_AsyncFunctionDef = _definition
        visit_ClassDef = _definition

        def visit_Lambda(self, child: ast.Lambda) -> None:
            return

        def _comprehension(self, child: ast.AST) -> None:
            # Comprehension targets have their own scope; walrus bindings do not.
            for item in ast.walk(child):
                if isinstance(item, ast.NamedExpr):
                    self.visit(item.target)

        visit_ListComp = _comprehension
        visit_SetComp = _comprehension
        visit_DictComp = _comprehension
        visit_GeneratorExp = _comprehension

        def visit_ExceptHandler(self, child: ast.ExceptHandler) -> None:
            if child.name:
                scope.writes.add(child.name)
                scope.bind(child.name, None)
            self.generic_visit(child)

        def visit_MatchAs(self, child: ast.MatchAs) -> None:
            if child.name:
                scope.bind(child.name, None)
            self.generic_visit(child)

        visit_MatchStar = visit_MatchAs

        def visit_MatchMapping(self, child: ast.MatchMapping) -> None:
            if child.rest:
                scope.bind(child.rest, None)
            self.generic_visit(child)

        def _external(self, child: Any) -> None:
            # Conservative across global/nonlocal writes; no flow/type inference.
            for name in child.names:
                scope.bind(name, None)

        visit_Global = _external
        visit_Nonlocal = _external

    binder = Binder()
    for child in _body(node):
        binder.visit(child)
    return scope


def _lookup(scope: _Scope, name: str) -> tuple[bool, tuple[str, str] | None]:
    current: _Scope | None = scope
    while current is not None:
        if name in current.bindings:
            return True, current.bindings[name]
        current = current.parent
        # A method (or nested class) does not close over class namespace names.
        while current is not None and isinstance(current.node, ast.ClassDef):
            current = current.parent
    return False, None


def extract_python(source: str, relative: str) -> list[dict[str, Any]]:
    tree = ast.parse(source, filename=relative)
    legacy_module = relative[:-3].replace("/", ".")
    module = legacy_module.removesuffix(".__init__")
    package = module if relative.endswith("/__init__.py") else module.rpartition(".")[0]
    root_scope = _scope(tree, module, None, package)
    rows: list[dict[str, Any]] = []

    def definition(node: Any, parent: _Scope, parents: list[str]) -> None:
        name = (
            node.name
            if not isinstance(node, ast.Lambda)
            else f"<lambda@{node.lineno}:{node.col_offset}>"
        )
        # Preserve every pre-existing Python symbol ID, including __init__ IDs.
        fqn = ".".join([legacy_module, *parents, name])
        lookup = ".".join([module, *parents, name])
        scope = _scope(node, lookup, parent, package)
        refs: set[str] = set()
        calls: list[dict[str, Any]] = []
        method_class = parent.lookup if isinstance(parent.node, ast.ClassDef) else ""
        receiver = ""
        if method_class and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [*node.args.posonlyargs, *node.args.args]
            decorators = {dotted_name(item) for item in node.decorator_list}
            if args and args[0].arg in {"self", "cls"} and "staticmethod" not in decorators:
                receiver = args[0].arg if args[0].arg not in scope.writes else ""

        def collect(
            child: ast.AST, hidden: frozenset[str] = frozenset(), call_scope: _Scope | None = None
        ) -> None:
            call_scope = call_scope or scope
            if isinstance(child, (*DEFINITIONS, ast.Lambda)):
                return  # Bodies belong only to their own symbol, never the parent.
            if isinstance(child, COMPREHENSIONS):
                bound = {
                    item.id
                    for gen in child.generators
                    for item in ast.walk(gen.target)
                    if isinstance(item, ast.Name)
                }
                for item in ast.iter_child_nodes(child):
                    collect(item, hidden | bound, call_scope)
                return
            if isinstance(child, ast.Call):
                text = dotted_name(child.func) or ast.unparse(child.func)
                refs.add(text)
                name = dotted_name(child.func)
                targets: list[str] = []
                reason, weak = "unknown_receiver", False
                if name:
                    parts = name.split(".")
                    found, binding = _lookup(call_scope, parts[0])
                    if parts[0] in hidden:
                        reason = "shadowed"
                    elif len(parts) == 2 and parts[0] == receiver and call_scope is scope:
                        targets, reason = [f"{method_class}.{parts[1]}"], "receiver"
                    elif found and binding is None:
                        reason = "shadowed"
                    elif binding is not None and (
                        len(parts) == 1 or binding[1] in {"import", "class"}
                    ):
                        targets = [".".join([binding[0], *parts[1:]])]
                        reason = "import" if binding[1] == "import" else "scope"
                    elif not found and len(parts) == 1:
                        reason, weak = "unbound_name", True
                calls.append(
                    {
                        "text": text,
                        "line": child.lineno,
                        "column": child.col_offset,
                        "targets": targets,
                        "reason": reason,
                        "weak_allowed": weak,
                    }
                )
            elif isinstance(child, ast.Import):
                refs.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                refs.add("." * child.level + (child.module or ""))
            for item in ast.iter_child_nodes(child):
                collect(item, hidden, call_scope)

        body = _body(node)
        for child in body:
            collect(child)
        # Definition-time expressions are retained on the definition, but resolve
        # in the enclosing namespace rather than against its parameters/locals.
        for child in ast.iter_child_nodes(node):
            if child not in body:
                collect(child, call_scope=parent)
        summary = (ast.get_docstring(node) or "") if not isinstance(node, ast.Lambda) else ""
        kind = (
            "python_class"
            if isinstance(node, ast.ClassDef)
            else "python_lambda"
            if isinstance(node, ast.Lambda)
            else "python_async_function"
            if isinstance(node, ast.AsyncFunctionDef)
            else "python_function"
        )
        rows.append(
            {
                "entity_id": f"symbol:{fqn}",
                "fqn": fqn,
                "name": name,
                "kind": kind,
                "path": relative,
                "line": node.lineno,
                "summary": summary.splitlines()[0][:240] if summary else "",
                "references": sorted(refs),
                "calls": calls,
                "navigation": {
                    "language": "python",
                    "module": module,
                    "lookup_fqn": lookup,
                    "end_line": node.end_lineno,
                    "scope": parent.lookup,
                    "bindings": {
                        key: list(value) if value else None
                        for key, value in sorted(scope.bindings.items())
                    },
                },
            }
        )
        discover(node, scope, [*parents, name])

    def discover(node: ast.AST, parent: _Scope, parents: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*DEFINITIONS, ast.Lambda)):
                definition(child, parent, parents)
            else:
                discover(child, parent, parents)

    discover(tree, root_scope, [])
    return rows
