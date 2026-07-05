"""Flake pin: no test module writes into the live ``src/teatree/`` tree.

A test that writes, creates, or deletes a file under the REAL ``src/teatree/``
tree (rather than under a ``tmp_path``) poisons every sibling xdist worker that
is concurrently enumerating, reading, or copying that tree — a single writer
failing N concurrent readers (the ``shutil.Error: No such file`` the tach-contract
``copytree`` used to race, the stale reads the scan-coverage gates used to hit).
The old ``_*_probe.py`` probes did exactly this. This meta-check AST-scans every
file under ``tests/`` and turns red on a write whose receiver path is anchored on
the repo root AND descends into ``src`` — so the shared-tree-mutation flake class
cannot return. A write anchored on ``tmp_path`` (or any non-repo-root name) is
correctly ignored.
"""

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"

_WRITE_METHODS = frozenset(
    {"write_text", "write_bytes", "touch", "mkdir", "symlink_to", "hardlink_to", "unlink", "rename", "replace", "rmdir"}
)


def _leftmost(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        node = node.left
    return node


def _has_src_segment(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Constant) and sub.value == "src" for sub in ast.walk(node))


def _root_and_src_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Module constants anchored on ``Path(__file__)…parents`` (root), and the subset descending into ``src``."""
    root_names: set[str] = set()
    src_names: set[str] = set()
    for _ in range(2):  # two passes so a name defined off another root name resolves transitively
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
                continue
            name = node.targets[0].id
            leftmost = _leftmost(node.value)
            anchored = "__file__" in ast.unparse(leftmost) or (
                isinstance(leftmost, ast.Name) and leftmost.id in root_names
            )
            if not anchored:
                continue
            root_names.add(name)
            if _has_src_segment(node.value) or (isinstance(leftmost, ast.Name) and leftmost.id in src_names):
                src_names.add(name)
    return root_names, src_names


def _write_receiver(node: ast.AST) -> ast.AST | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _WRITE_METHODS:
        return func.value
    if isinstance(func, ast.Name) and func.id == "open" and len(node.args) >= 2:
        mode = node.args[1]
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(ch in mode.value for ch in "wax"):
            return node.args[0]
    return None


def writes_into_live_src(source: str) -> list[str]:
    """Receiver expressions of write calls anchored on the repo root and descending into ``src``."""
    tree = ast.parse(source)
    root_names, src_names = _root_and_src_names(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        receiver = _write_receiver(node)
        if receiver is None:
            continue
        leftmost = _leftmost(receiver)
        if not isinstance(leftmost, ast.Name):
            continue
        if leftmost.id in src_names or (leftmost.id in root_names and _has_src_segment(receiver)):
            offenders.append(ast.unparse(receiver))
    return offenders


# Whole-tree AST scan of every tests/**/*.py — headroom over the 60s default
# pytest-timeout so it never trips under concurrent-coder load in the push hook.
@pytest.mark.timeout(300)
class TestNoTestWritesUnderSrc:
    def test_no_test_module_writes_into_the_live_src_tree(self) -> None:
        offenders: dict[str, list[str]] = {}
        for py in sorted(_TESTS_ROOT.rglob("*.py")):
            hits = writes_into_live_src(py.read_text(encoding="utf-8"))
            if hits:
                offenders[py.relative_to(_REPO_ROOT).as_posix()] = hits
        assert not offenders, (
            "test module(s) write into the live src/teatree tree — a single writer poisons every "
            f"concurrent xdist reader (copytree/scan races). Write probes under tmp_path instead:\n{offenders}"
        )


class TestDetectorAntiVacuity:
    def test_flags_a_repo_root_anchored_src_write(self) -> None:
        snippet = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parents[2]\n"
            "def test_x():\n"
            "    (_REPO_ROOT / 'src' / 'teatree' / '_probe.py').write_text('x')\n"
        )
        assert writes_into_live_src(snippet) == ["_REPO_ROOT / 'src' / 'teatree' / '_probe.py'"]

    def test_flags_a_src_named_constant_write(self) -> None:
        snippet = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parents[2]\n"
            "_SRC = _REPO_ROOT / 'src' / 'teatree'\n"
            "def test_x():\n"
            "    (_SRC / '_probe.py').unlink()\n"
        )
        assert writes_into_live_src(snippet) == ["_SRC / '_probe.py'"]

    def test_flags_open_write_into_src(self) -> None:
        snippet = (
            "from pathlib import Path\n"
            "_ROOT = Path(__file__).resolve().parents[2]\n"
            "def test_x():\n"
            "    open(_ROOT / 'src' / 'teatree' / '_probe.py', 'w')\n"
        )
        assert writes_into_live_src(snippet)

    def test_ignores_tmp_path_src_write(self) -> None:
        # over-block guard: a write under tmp_path (a non-repo-root name) is clean,
        # even when the path descends into a `src` segment.
        snippet = "def test_x(tmp_path):\n    (tmp_path / 'src' / 'teatree' / '_probe.py').write_text('x')\n"
        assert writes_into_live_src(snippet) == []

    def test_ignores_repo_root_read_and_non_src_write(self) -> None:
        snippet = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parents[2]\n"
            "def test_x():\n"
            "    (_REPO_ROOT / 'src' / 'teatree' / 'x.py').read_text()\n"  # read, not write
            "    (_REPO_ROOT / 'tests' / 'scratch.py').write_text('x')\n"  # write, but not into src
        )
        assert writes_into_live_src(snippet) == []
