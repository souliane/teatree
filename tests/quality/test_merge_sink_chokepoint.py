"""Merge-sink chokepoint fitness function (#1985).

The ONLY sanctioned squash-merge primitive is ``execute_bound_merge`` in
``teatree.core.merge`` (which binds the merge to the reviewed SHA and
runs ``assert_merge_preconditions``). Its raw forge argv lives in
``teatree.backends.forge_merge_rpc``. Any OTHER module that constructs a raw
unbound squash-merge call (``gh pr merge --squash``) or hits a GitLab
``merge_requests/<id>/merge`` endpoint bypasses the SHA-bind + live-draft +
live-CI re-checks — the exact #1985 bypass class.

This AST fitness test walks ``src/teatree/`` and flags any such call outside the
allowed transport homes. It is the durable catch-all: a future re-introduction of
a raw squash-merge anywhere goes RED here.
"""

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "teatree"

# The sole modules allowed to carry raw forge merge argv: the merge-transport
# home and the keystone primitive that delegates to it.
_ALLOWED_MODULES = frozenset(
    {
        "teatree.backends.forge_merge_rpc",
    },
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(_SRC_ROOT.parent).with_suffix("")
    return ".".join(rel.parts)


def _string_literals(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _is_unbound_squash_list(node: ast.AST) -> bool:
    """True iff *node* is an argv list literal building a raw squash-merge call.

    Catches the ``["pr", "merge", …, "--squash"]`` shape whether it is inlined
    into a call or assigned to a variable first (``argv = [...]``).
    """
    if not isinstance(node, ast.List):
        return False
    literals = _string_literals(node)
    return "merge" in literals and "--squash" in literals


def _is_merge_endpoint(node: ast.AST) -> bool:
    """True iff *node* is a GitLab ``merge_requests/<id>/merge`` endpoint string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "/merge_requests/" in node.value and node.value.endswith("/merge")
    # f-string: ``projects/<...>/merge_requests/<...>/merge``
    if isinstance(node, ast.JoinedStr):
        literal_parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        joined = "".join(literal_parts)
        return "/merge_requests/" in joined and joined.endswith("/merge")
    return False


def _offending_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted(
        {node.lineno for node in ast.walk(tree) if _is_unbound_squash_list(node) or _is_merge_endpoint(node)},
    )


def test_no_unbound_squash_merge_outside_the_transport_home() -> None:
    offenders: dict[str, list[int]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if _module_name(path) in _ALLOWED_MODULES:
            continue
        lines = _offending_calls(path)
        if lines:
            offenders[str(path.relative_to(_SRC_ROOT.parent))] = lines
    assert not offenders, (
        "Raw unbound squash-merge / merge-endpoint call outside the transport home "
        f"(forge_merge_rpc) — bypasses the SHA-bind merge keystone (#1985): {offenders}"
    )
