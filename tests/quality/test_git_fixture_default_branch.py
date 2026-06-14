"""Fitness function: real-git fixtures never assume the ambient default branch.

The fragility class (souliane/teatree#2359): a real-git fixture does a bare
``git init`` (no ``-b`` / ``--initial-branch``) and then references a literal
branch — ``worktree add <path> main``, ``checkout main``, ``push origin main``,
``rev-parse main``. It passes on a dev box whose config bakes in
``init.defaultBranch=main`` but exits 128 (``invalid reference: main``) on a CI
image whose git defaults to ``master``, red-flagging the full-suite job on PRs
whose own diff is unrelated.

The fix is to never rely on the ambient default: born the branch with
``git init -b <name>`` (or ``--initial-branch=<name>``), rename it
deterministically with ``git branch -M <name>``, or build the repo through
:func:`tests._git_repo.make_git_repo`. This gate makes a regression mechanical
instead of waiting for the next red-CI incident.

The scanner walks every test ``.py`` for a ``git init`` invocation — either a
subprocess argument list (``[..., "git", "init", ...]``) or a varargs git
helper call (``_git(repo, "init", ...)`` / ``run_git(repo, "init", ...)``) —
and flags one that specifies neither ``-b`` / ``--initial-branch`` (born the
branch) nor ``--bare`` (a bare repo has no working branch to assume).

The deterministic-rename idiom is exempt: a bare ``git init`` whose enclosing
function also names the branch ``main`` deterministically — ``git branch -M
main`` / ``git branch -m main`` (rename after init), ``git checkout -b main``,
or ``git switch -c main`` — is safe, because the literal ``main`` ref is created
regardless of the host's ``init.defaultBranch``. The exemption is scoped to the
enclosing function: a bare init at module scope, or one in a function that has
no such rename, is still flagged. Repo paths in real fixtures are runtime
expressions (``str(p)``, ``-C str(p)``), so the exemption is function-scoped
rather than per-repo-path-correlated — a deliberately mixed function that bare-
inits one repo while renaming a *different* repo would be under-flagged, but
that shape does not occur and the function scope is the reliable seam.

A deliberate bare init with no rename declares intent with a
``# git-fixture: default-branch-ok`` pragma on the call line.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"

_PRAGMA = "git-fixture: default-branch-ok"

# A ``git init`` that bornes or renames the branch is safe.
_BORN_FLAGS = ("-b", "--initial-branch")
_BARE_FLAGS = ("--bare",)

# The default branch a deterministic rename must target for the init to be exempt.
_RENAME_TARGET_BRANCH = "main"

# (subcommand, flag) pairs that deterministically name the current branch, so a
# preceding bare ``git init`` in the same function does not assume the ambient
# default: ``git branch -M/-m main`` (rename), ``git checkout -b main`` /
# ``git switch -c main`` (create-and-switch).
_RENAME_IDIOMS = (
    ("branch", ("-M", "-m")),
    ("checkout", ("-b",)),
    ("switch", ("-c",)),
)

# Global git flags that take a following value, so the subcommand is not the very
# next token (``git -C <path> init`` → the subcommand is ``init``, not ``<path>``).
_VALUE_TAKING_GLOBAL_FLAGS = ("-C", "--git-dir", "--work-tree", "--namespace", "-c")

# Varargs git-helper call names across the test packages (``_git(repo, "init", …)``,
# ``run_git(repo, "init", …)``, ``self._git(repo, "init", …)``).
_GIT_HELPER_NAMES = ("_git", "run_git", "_run_git")

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "git_fixture_default_branch"
_MUST_FLAG = sorted((_FIXTURES / "must_flag").glob("*.py.txt"))
_MUST_NOT_FLAG = sorted((_FIXTURES / "must_not_flag").glob("*.py.txt"))


@dataclass(frozen=True)
class GitInitFinding:
    """A ``git init`` call that assumes the ambient default branch."""

    path: Path
    lineno: int


def _arg_value(node: ast.expr) -> str | None:
    """A positional arg's constant-string value, or ``None`` for a runtime expression.

    Positions are preserved (``None`` placeholders) so a value-taking global flag
    such as ``-C <runtime-path>`` keeps its pairing — otherwise dropping the
    non-constant path would misalign the skip and mis-read the subcommand.
    """
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _call_args(node: ast.Call) -> list[str | None]:
    """Positional args of a call, constants as strings and runtime exprs as ``None``."""
    return [_arg_value(a) for a in node.args]


def _git_list_args(node: ast.List) -> list[str | None] | None:
    """Arg vector after the literal ``"git"`` element, or ``None`` if not a git vector."""
    elems = [_arg_value(e) for e in node.elts]
    if "git" not in elems:
        return None
    return elems[elems.index("git") + 1 :]


def _has_born_or_bare_flag(args: list[str | None]) -> bool:
    """Whether the vector borns the branch (``-b`` / ``--initial-branch[=…]``) or is bare."""
    for token in args:
        if token is None:
            continue
        if token in _BORN_FLAGS or token in _BARE_FLAGS:
            return True
        if token.startswith(("--initial-branch=", "--bare")):
            return True
    return False


def _git_subcommand(args: list[str | None]) -> str | None:
    """The git subcommand verb in an arg vector, skipping value-taking global flags.

    ``["-C", "<path>", "init", "-q"]`` → ``init``. A ``-m "init"`` message is the
    value of ``-m``, not the verb, so ``["commit", "-m", "init"]`` → ``commit``.
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token is not None and token in _VALUE_TAKING_GLOBAL_FLAGS:
            i += 2
            continue
        if token is None or token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _init_assumes_default_branch(args: list[str | None]) -> bool:
    """A git arg vector whose subcommand is ``init`` without borning or declaring bare."""
    if _git_subcommand(args) != "init":
        return False
    return not _has_born_or_bare_flag(args)


def _renames_branch_to_main(args: list[str | None]) -> bool:
    """A git arg vector that deterministically names the current branch ``main``.

    Matches ``branch -M/-m main``, ``checkout -b main``, and ``switch -c main``
    — the create/rename idioms that make ``main`` a real ref after a bare init.
    """
    subcommand = _git_subcommand(args)
    rest = args[args.index(subcommand) + 1 :] if subcommand in args else []
    for verb, flags in _RENAME_IDIOMS:
        if subcommand != verb:
            continue
        for i, token in enumerate(rest):
            if token in flags and rest[i + 1 :] and rest[i + 1] == _RENAME_TARGET_BRANCH:
                return True
    return False


def _call_helper_name(node: ast.Call) -> str | None:
    """The dotted-or-bare callee name of a call (``_git``, ``self._git``)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _git_vector(node: ast.AST) -> list[str | None] | None:
    """The git arg vector a node represents, or ``None`` if it is not a git call.

    A subprocess argument list (``["git", "init", …]``) or a varargs git-helper
    call (``_git(repo, "init", …)``).
    """
    if isinstance(node, ast.List):
        return _git_list_args(node)
    if isinstance(node, ast.Call) and _call_helper_name(node) in _GIT_HELPER_NAMES:
        return _call_args(node)
    return None


def _enclosing_scopes(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map each function-defining node in *tree* to the function it is nested in.

    The module itself maps to itself; a node's nearest enclosing function (or the
    module) is the scope its rename idiom is correlated within.
    """
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _nearest_function_scope(node: ast.AST, parent: dict[ast.AST, ast.AST], module: ast.AST) -> ast.AST:
    """The nearest enclosing ``def``/``async def`` of *node*, or the module itself."""
    current = parent.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
        current = parent.get(current)
    return module


def scan_source(source: str, path: Path) -> list[GitInitFinding]:
    """Findings for one parsed source string.

    A bare ``git init`` is exempt when its enclosing function also names the
    branch ``main`` deterministically (the rename idiom) — see the module
    docstring for why the correlation is function-scoped, not per-repo-path.
    """
    tree = ast.parse(source)
    pragma = {i for i, line in enumerate(source.splitlines(), start=1) if _PRAGMA in line}
    parent = _enclosing_scopes(tree)
    rename_scopes: set[ast.AST] = set()
    for node in ast.walk(tree):
        vector = _git_vector(node)
        if vector is not None and _renames_branch_to_main(vector):
            rename_scopes.add(_nearest_function_scope(node, parent, tree))
    findings: list[GitInitFinding] = []
    for node in ast.walk(tree):
        vector = _git_vector(node)
        if vector is None or not _init_assumes_default_branch(vector):
            continue
        if node.lineno in pragma:
            continue
        if _nearest_function_scope(node, parent, tree) in rename_scopes:
            continue
        findings.append(GitInitFinding(path, node.lineno))
    return findings


def scan_file(path: Path) -> list[GitInitFinding]:
    """Findings for one file (``.py`` or a ``.py.txt`` corpus fixture)."""
    return scan_source(path.read_text(encoding="utf-8"), path)


def scan_tree(root: Path) -> list[GitInitFinding]:
    """Findings across every ``*.py`` test module under ``root``."""
    if not root.exists():
        return []
    findings: list[GitInitFinding] = []
    for py in sorted(root.rglob("*.py")):
        findings.extend(scan_file(py))
    return findings


class TestLiveTree:
    def test_no_real_git_fixture_assumes_the_default_branch(self) -> None:
        findings = scan_tree(_TESTS_ROOT)
        assert not findings, (
            "real-git fixture(s) run `git init` without `-b`/`--initial-branch` (or `--bare`) — "
            "born the branch (`git init -b main`), rename it (`git branch -M main`), or build the "
            "repo with `tests._git_repo.make_git_repo` (add a `# git-fixture: default-branch-ok` "
            "pragma only for a deliberate bare init):\n"
            + "\n".join(f"  {f.path.relative_to(_REPO_ROOT)}:{f.lineno}" for f in findings)
        )


class TestScanner:
    def test_bare_init_subprocess_list_is_flagged(self) -> None:
        src = 'subprocess.run(["git", "init", "-q", str(p)], check=True)\n'
        findings = scan_source(src, Path("x.py"))
        assert len(findings) == 1

    def test_init_with_dash_b_is_not_flagged(self) -> None:
        src = 'subprocess.run(["git", "init", "-q", "-b", "main", str(p)], check=True)\n'
        assert scan_source(src, Path("x.py")) == []

    def test_init_with_initial_branch_value_form_is_not_flagged(self) -> None:
        src = 'subprocess.run(["git", "init", "--initial-branch=main", str(p)], check=True)\n'
        assert scan_source(src, Path("x.py")) == []

    def test_helper_init_with_initial_branch_value_form_is_not_flagged(self) -> None:
        src = '_git(main, "init", "--initial-branch=main", "-q")\n'
        assert scan_source(src, Path("x.py")) == []

    def test_commit_with_runtime_dash_c_path_is_not_flagged(self) -> None:
        # ``git -C <runtime-path> commit -m init`` — the verb is commit; the
        # ``init`` is the message. The runtime ``-C`` value must not misalign it.
        src = 'subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)\n'
        assert scan_source(src, Path("x.py")) == []

    def test_init_with_runtime_dash_c_path_is_flagged(self) -> None:
        src = 'subprocess.run(["git", "-C", str(p), "init", "-q"], check=True)\n'
        assert len(scan_source(src, Path("x.py"))) == 1

    def test_bare_repo_init_is_not_flagged(self) -> None:
        src = 'subprocess.run(["git", "init", "-q", "--bare", str(p)], check=True)\n'
        assert scan_source(src, Path("x.py")) == []

    def test_bare_helper_init_is_flagged(self) -> None:
        src = '_git(repo, "init", "-q")\n'
        findings = scan_source(src, Path("x.py"))
        assert len(findings) == 1

    def test_helper_init_with_dash_b_is_not_flagged(self) -> None:
        src = '_git(repo, "init", "-q", "-b", "main")\n'
        assert scan_source(src, Path("x.py")) == []

    def test_method_helper_init_is_scanned(self) -> None:
        src = 'self._git(repo, "init", "-q")\n'
        assert len(scan_source(src, Path("x.py"))) == 1

    def test_run_git_helper_init_is_scanned(self) -> None:
        src = 'run_git("init", "-q", cwd=repo)\n'
        assert len(scan_source(src, Path("x.py"))) == 1

    def test_non_init_git_list_is_not_flagged(self) -> None:
        src = 'subprocess.run(["git", "commit", "-m", "init"], check=True)\n'
        assert scan_source(src, Path("x.py")) == []

    def test_non_git_list_with_init_word_is_not_flagged(self) -> None:
        # An arg vector without "git" is some other tool — never a git-init fixture.
        src = 'subprocess.run(["docker", "init"], check=True)\n'
        assert scan_source(src, Path("x.py")) == []

    def test_pragma_opts_out_a_deliberate_bare_init(self) -> None:
        src = f'subprocess.run(["git", "init", str(p)], check=True)  # {_PRAGMA}\n'
        assert scan_source(src, Path("x.py")) == []

    def test_bare_init_then_branch_rename_to_main_is_not_flagged(self) -> None:
        src = (
            "def setup(p):\n"
            '    subprocess.run(["git", "init", "-q", str(p)], check=True)\n'
            '    subprocess.run(["git", "-C", str(p), "branch", "-M", "main"], check=True)\n'
        )
        assert scan_source(src, Path("x.py")) == []

    def test_bare_helper_init_then_branch_rename_to_main_is_not_flagged(self) -> None:
        src = 'def setup(repo):\n    _git(repo, "init", "-q")\n    _git(repo, "branch", "-m", "main")\n'
        assert scan_source(src, Path("x.py")) == []

    def test_bare_init_then_checkout_new_main_is_not_flagged(self) -> None:
        src = 'def setup(repo):\n    _git(repo, "init", "-q")\n    _git(repo, "checkout", "-b", "main")\n'
        assert scan_source(src, Path("x.py")) == []

    def test_bare_init_then_switch_new_main_is_not_flagged(self) -> None:
        src = 'def setup(repo):\n    _git(repo, "init", "-q")\n    _git(repo, "switch", "-c", "main")\n'
        assert scan_source(src, Path("x.py")) == []

    def test_bare_init_with_no_rename_is_still_flagged(self) -> None:
        src = 'def setup(p):\n    subprocess.run(["git", "init", str(p)], check=True)\n'
        assert len(scan_source(src, Path("x.py"))) == 1

    def test_rename_in_a_different_function_does_not_exempt(self) -> None:
        src = (
            "def setup(p):\n"
            '    subprocess.run(["git", "init", str(p)], check=True)\n'
            "\n"
            "def other(p):\n"
            '    subprocess.run(["git", "-C", str(p), "branch", "-M", "main"], check=True)\n'
        )
        assert len(scan_source(src, Path("x.py"))) == 1

    def test_rename_to_a_non_main_branch_does_not_exempt(self) -> None:
        src = 'def setup(p):\n    _git(p, "init", "-q")\n    _git(p, "branch", "-M", "trunk")\n'
        assert len(scan_source(src, Path("x.py"))) == 1

    def test_module_scope_bare_init_is_flagged_despite_a_function_rename(self) -> None:
        src = (
            'subprocess.run(["git", "init", str(p)], check=True)\n'
            "\n"
            "def setup(p):\n"
            '    subprocess.run(["git", "-C", str(p), "branch", "-M", "main"], check=True)\n'
        )
        assert len(scan_source(src, Path("x.py"))) == 1

    def test_finding_carries_location(self) -> None:
        src = '\n\nsubprocess.run(["git", "init", str(p)], check=True)\n'
        findings = scan_source(src, Path("y.py"))
        assert len(findings) == 1
        assert findings[0].lineno == 3
        assert findings[0].path == Path("y.py")

    def test_scan_tree_skips_nonexistent_root(self) -> None:
        assert scan_tree(_REPO_ROOT / "does_not_exist") == []


class TestGoldenCorpus:
    def test_corpus_has_both_dimensions(self) -> None:
        assert _MUST_FLAG, "must-FLAG corpus is empty"
        assert _MUST_NOT_FLAG, "must-NOT-FLAG corpus is empty (over-block dimension missing)"

    @pytest.mark.parametrize("fixture", _MUST_FLAG, ids=[p.stem for p in _MUST_FLAG])
    def test_must_flag_fixture_is_flagged(self, fixture: Path) -> None:
        assert scan_file(fixture), f"{fixture.name} should produce a finding but did not"

    @pytest.mark.parametrize("fixture", _MUST_NOT_FLAG, ids=[p.stem for p in _MUST_NOT_FLAG])
    def test_must_not_flag_fixture_is_not_flagged(self, fixture: Path) -> None:
        findings = scan_file(fixture)
        assert not findings, f"{fixture.name} wrongly flagged at lines: " + ", ".join(str(f.lineno) for f in findings)
