"""No forge CLI call in ``teatree.core`` may run without the writer's credential.

souliane/teatree#4116: the open-PR probe shelled ``gh`` with a bare environment
while the writer path handed the same binary a ``GH_TOKEN``. On any repo the
ambient identity cannot read, that probe exits non-zero — and a caller reading
the resulting can't-tell as "no PR" refused every SECOND push to a branch whose
PR already existed. Fixing that one call site leaves the class intact: the same
omission sits one edit away in every other ``gh`` / ``glab`` invocation.

The rule is one line at each call site — ``env=forge_cli_env()`` — so this walk
pins it. Two whole packages are exempt because they already solve it another way:
``teatree.backends`` threads an explicit token through its own runner
(``_run_gh``), and ``teatree.loop.scanners`` builds ``{**os.environ, "GH_TOKEN":
…}`` from its scanner factory.

Reach is a literal argv whose first element is ``gh`` or ``glab``, passed to a
:mod:`teatree.utils.run` wrapper. An argv assembled elsewhere is out of an AST
walk's reach — and is also not the shape that regrows, since every call site here
writes its argv inline.
"""

import ast
from pathlib import Path

from tests.conformance._src_tree import SRC_DIR, parsed_modules

_CORE_DIR = SRC_DIR / "core"
_FORGE_BINARIES = frozenset({"gh", "glab"})
_RUNNERS = frozenset({"run_checked", "run_allowed_to_fail", "run_streamed"})
# Below this the walk cannot have been looking at the real source at all.
_MIN_FORGE_CALL_SITES = 8


def _leading_binary(call: ast.Call) -> str:
    """The argv's first element when it is a literal forge binary name, else ``""``."""
    if not call.args:
        return ""
    argv = call.args[0]
    if not isinstance(argv, ast.List | ast.Tuple) or not argv.elts:
        return ""
    first = argv.elts[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return ""
    return first.value if first.value in _FORGE_BINARIES else ""


def _runner_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def forge_cli_call_sites() -> list[tuple[Path, int, bool]]:
    """Every literal ``gh`` / ``glab`` subprocess call in core as ``(path, line, has_env)``."""
    sites: list[tuple[Path, int, bool]] = []
    for path, tree in parsed_modules(_CORE_DIR):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _runner_name(node) not in _RUNNERS:
                continue
            if not _leading_binary(node):
                continue
            has_env = any(keyword.arg == "env" for keyword in node.keywords)
            sites.append((path, node.lineno, has_env))
    return sites


def test_the_walk_still_sees_the_forge_call_sites() -> None:
    assert len(forge_cli_call_sites()) >= _MIN_FORGE_CALL_SITES


def test_every_core_forge_cli_call_carries_a_credential() -> None:
    bare = [(path, line) for path, line, has_env in forge_cli_call_sites() if not has_env]
    assert not bare, (
        "these forge CLI calls run without the writer path's credential — pass "
        "`env=forge_cli_env()` so the read sees what the write sees:\n"
        + "\n".join(f"  {path.relative_to(SRC_DIR.parent.parent)}:{line}" for path, line in bare)
    )
