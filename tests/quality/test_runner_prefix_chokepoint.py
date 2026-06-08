"""Runner-prefix chokepoint fitness function (#2004).

The ONLY sanctioned site that emits the manage.py interpreter prefix
(``uv --directory <repo> run python`` / ``pipenv run python``) is
``runner_prefix`` in ``teatree.utils.django_db``. That one helper owns the
pipenv-vs-uv dependency-manager detection (#1973); every other call site must
route through it.

PR #1976 regressed this: core assumed ``uv run`` universally for the
reference-DB migrate while the overlay hand-rolled an unconditional
``uv --directory`` prefix in ``cli.overlay.uv_cmd`` — two divergent
interpreter-prefix implementations that drifted apart (#1973).

This AST fitness test walks ``src/teatree/`` and flags any literal building an
interpreter prefix for a Python / ``manage.py`` invocation outside the allowed
home. It is the durable catch-all: a future re-introduction of a hand-rolled
prefix anywhere goes RED here.
"""

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "teatree"

# The sole module allowed to construct the manage.py interpreter prefix: the
# manager-aware runner-prefix helper (#1973).
_ALLOWED_MODULES = frozenset({"teatree.utils.django_db"})

_MANAGER_TOKENS = frozenset({"uv", "pipenv"})
_INTERPRETER_TOKENS = frozenset({"python", "manage.py"})


def _module_name(path: Path) -> str:
    rel = path.relative_to(_SRC_ROOT.parent).with_suffix("")
    return ".".join(rel.parts)


def _str_literals(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _is_interpreter_prefix_seq(node: ast.AST) -> bool:
    """True iff *node* is a list/tuple literal building a manage.py interpreter prefix.

    Two shapes both count, mirroring the only two the codebase ever uses.

    Shape A — ``[..., "--directory", <repo>, "run", ...]`` — the uv
    directory-isolation prefix. The discriminator is the ``--directory`` +
    ``run`` pair, which is unique to running an isolated repo environment; the
    ``uv`` binary itself may be a literal or resolved via ``shutil.which`` into
    the list head, so keying on a ``"uv"`` literal would miss the
    resolved-binary form.

    Shape B — ``["uv"/"pipenv", "run", ..., "python"/"manage.py", ...]`` — a
    manager ``run`` that targets the Python interpreter or ``manage.py``
    directly.

    A bare ``["uv", "run", "pytest"]`` / ``["uv", "sync"]`` / ``["uv", "pip", …]``
    tooling command is deliberately NOT flagged: it does not invoke the app's
    Python interpreter, so it does not depend on the pipenv-vs-uv detection.
    """
    if not isinstance(node, ast.List | ast.Tuple):
        return False
    literals = set(_str_literals(node))
    if "--directory" in literals and "run" in literals:
        return True
    if not literals & _MANAGER_TOKENS:
        return False
    return "run" in literals and bool(literals & _INTERPRETER_TOKENS)


def _offending_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted({node.lineno for node in ast.walk(tree) if _is_interpreter_prefix_seq(node)})


def test_no_interpreter_prefix_literal_outside_the_runner_prefix_helper() -> None:
    offenders: dict[str, list[int]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if _module_name(path) in _ALLOWED_MODULES:
            continue
        lines = _offending_lines(path)
        if lines:
            offenders[str(path.relative_to(_SRC_ROOT.parent))] = lines
    assert not offenders, (
        "Hand-rolled manage.py interpreter prefix (uv --directory / pipenv run python) "
        "outside the runner-prefix helper (teatree.utils.django_db.runner_prefix) — "
        f"a second runner silently diverges from the pipenv-vs-uv detection (#1976, #1973): {offenders}"
    )


@pytest.mark.parametrize(
    "source",
    [
        '["uv", "--directory", repo, "run", "python"]',
        '[uv, "--directory", str(p), "run", "python", "manage.py"]',
        '["pipenv", "run", "python"]',
        '("uv", "run", "manage.py", "migrate")',
        '["uv", "run", "python", "-m", "pytest"]',
    ],
)
def test_predicate_flags_interpreter_prefix_shapes(source: str) -> None:
    node = ast.parse(source, mode="eval").body
    assert _is_interpreter_prefix_seq(node)


@pytest.mark.parametrize(
    "source",
    [
        '["uv", "run", "pytest"]',
        '["uv", "sync"]',
        '["uv", "pip", "install", "-e", str(p)]',
        '("uv", "run", "--group", "mutation", "mutmut")',
        '["uv", "run", "t3", "--help"]',
        '["createdb", "-h", host]',
        '["python", "manage.py", "migrate"]',
    ],
)
def test_predicate_ignores_tooling_and_non_isolated_commands(source: str) -> None:
    node = ast.parse(source, mode="eval").body
    assert not _is_interpreter_prefix_seq(node)
