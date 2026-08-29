"""Single-writer guarantee for ``SessionState``'s protected fields.

``StateReducer.apply`` is documented (state.py, design.md §3/§5.4/§5.6) as
the only code path allowed to mutate ``SessionState.constraints`` /
``session_profile`` and the Router/Value-Node graph's new fields
(``candidates``, ``ranked``, ``details_cache``, ``pending_question``,
``node_trace``, ``search_retry_count``).  This is a static, AST-based check
rather than a runtime behavior test: it proves no *other* module in the
package ever assigns to, or in-place mutates, one of those attributes,
regardless of which code path is exercised at runtime.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The reducer owns writes to these SessionState attributes.  Chosen to be a
# distinctive namespace (verified empty-collision with any other attribute
# of the same name elsewhere in the tracked source tree) so an attribute-name
# heuristic does not need type inference to be reliable here.
PROTECTED_FIELDS = frozenset(
    {
        "constraints",
        "session_profile",
        "candidates",
        "ranked",
        "details_cache",
        "pending_question",
        "node_trace",
        "search_retry_count",
    }
)

# Only the reducer's own module may perform these writes.
ALLOWED_WRITER_FILE = REPO_ROOT / "starter" / "shopping_agent" / "state.py"

# In-place mutation on a protected field (e.g. ``state.constraints.append(x)``)
# is exactly as much a bypass of the single commit path as reassignment.
MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "update",
        "discard",
        "add",
        "setdefault",
        "popitem",
        "__setitem__",
        "__delitem__",
    }
)

SCAN_DIRECTORIES = ("starter", "evaluator", "tests")


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCAN_DIRECTORIES:
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


class _ProtectedFieldWriteVisitor(ast.NodeVisitor):
    """Collects (lineno, description) for any write to a protected field."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def _flag(self, node: ast.AST, description: str) -> None:
        self.violations.append((getattr(node, "lineno", -1), description))

    def _check_target(self, target: ast.expr) -> None:
        if isinstance(target, ast.Attribute) and target.attr in PROTECTED_FIELDS:
            self._flag(target, f"assignment to .{target.attr}")
        elif isinstance(target, ast.Subscript):
            value = target.value
            if isinstance(value, ast.Attribute) and value.attr in PROTECTED_FIELDS:
                self._flag(target, f"subscript assignment into .{value.attr}[...]")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._check_target(element)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - ast API
        for target in node.targets:
            self._check_target(target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802 - ast API
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - ast API
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802 - ast API
        for target in node.targets:
            self._check_target(target)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in MUTATING_METHODS
            and isinstance(func.value, ast.Attribute)
            and func.value.attr in PROTECTED_FIELDS
        ):
            self._flag(node, f"in-place mutation via .{func.value.attr}.{func.attr}(...)")
        self.generic_visit(node)


class StateReducerSingleWriterTest(unittest.TestCase):
    def test_no_code_path_other_than_state_reducer_writes_protected_fields(self) -> None:
        violations: list[str] = []

        # 1) Every other tracked module (starter/, evaluator/, tests/) must
        #    never assign to, or in-place mutate, a protected field.
        for path in _iter_python_files():
            if path.resolve() == ALLOWED_WRITER_FILE.resolve():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            visitor = _ProtectedFieldWriteVisitor()
            visitor.visit(tree)
            for lineno, description in visitor.violations:
                violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {description}")

        # 2) Within state.py itself, protected-field writes must live inside
        #    StateReducer's own methods -- otherwise this file would quietly
        #    grow a second write path under the same "allowed" file.
        source = ALLOWED_WRITER_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ALLOWED_WRITER_FILE))
        reducer_class = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "StateReducer"
        )
        reducer_line_range = range(
            reducer_class.lineno, (reducer_class.end_lineno or reducer_class.lineno) + 1
        )
        module_visitor = _ProtectedFieldWriteVisitor()
        module_visitor.visit(tree)
        for lineno, description in module_visitor.violations:
            if lineno not in reducer_line_range:
                violations.append(f"starter/shopping_agent/state.py:{lineno}: {description} (outside StateReducer)")

        self.assertEqual(
            violations,
            [],
            "Only StateReducer's own methods (starter/shopping_agent/state.py) may "
            "write SessionState's protected fields; found direct writes:\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
