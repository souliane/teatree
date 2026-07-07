"""Single source of truth for "what changed" + "does anything force FULL" (#122).

Both the push gate (:mod:`teatree.quality.push_gate`) and the CI-lane classifier
(``scripts/ci/changed_lanes.py``) consume ONE changed-files + FULL-trigger
classifier so CI-lane routing and push-gate routing never drift (architecture
check #8 — one normalizer). The FULL-trigger config sets live HERE and
``changed_lanes`` imports them back, so a single definition drives both consumers.

Doctrine (mirrors ``changed_lanes`` + souliane/teatree#132): over-run is free,
under-run is a false green. Every uncertainty ⇒ FULL. Scoping is the exception,
taken only when the diff is provably classifiable; the whole-tree run is the
default branch. The module imports only the shared subprocess helper (no Django),
so it stays a safe leaf importable from the dependency-light CI script.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail

# Hoisted from ``changed_lanes`` so lane routing and push routing share one
# definition. ``changed_lanes`` imports these back; the conformance test
# ``tests/quality/test_changed_set_classifier.py`` pins the two modules agree.
CONFIG_EXACT: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        ".pre-commit-config.yaml",
        "manage.py",
    }
)
CONFIG_PREFIXES: tuple[str, ...] = (".github/", ".ast-grep/", "dev/")
CONFIG_SUFFIXES: tuple[str, ...] = (".toml", ".lock", ".cfg", ".ini")

# The two roots the push-gate sweeps model: doctest reads ``src/teatree/**`` module
# docstrings; the ast-grep blocking rules match ``.py`` under ``src`` + ``tests``.
_SRC_ROOT = "src/teatree/"
_TESTS_ROOT = "tests/"
_PYTHON_SUFFIXES: tuple[str, ...] = (".py", ".pyi")
_DOC_SUFFIXES: tuple[str, ...] = (".md", ".markdown")
_MANIFEST_FILES: frozenset[str] = frozenset(
    {
        "src/teatree/quality/regression_catalog.py",
        "src/teatree/quality/regression_rules.yaml",
    }
)
# git ``--name-status`` letters that mean the current tree no longer holds the
# node's edges/content (delete / rename / copy / type-change) — always FULL.
_DESTRUCTIVE_STATUS: frozenset[str] = frozenset({"D", "R", "C", "T"})


class ChangedSetError(RuntimeError):
    """Raised when the changed set cannot be computed (dirty/shallow merge-base)."""


@dataclass(frozen=True)
class ChangeEntry:
    """One changed path and its git ``--name-status`` letter (``R``/``C`` = new path)."""

    status: str
    path: str


@dataclass(frozen=True)
class ChangedSet:
    entries: tuple[ChangeEntry, ...]
    base_ref: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)

    @property
    def has_delete_or_rename(self) -> bool:
        return any(entry.status in _DESTRUCTIVE_STATUS for entry in self.entries)


@dataclass(frozen=True)
class FullTrigger:
    """The classification verdict: whole-tree FULL, or the scoped file lists."""

    full: bool
    reason: str
    scoped_src: tuple[Path, ...] = ()
    scoped_tests: tuple[Path, ...] = ()


def _is_python(path: str) -> bool:
    return path.endswith(_PYTHON_SUFFIXES)


def _is_config(path: str) -> bool:
    return path in CONFIG_EXACT or path.startswith(CONFIG_PREFIXES) or path.endswith(CONFIG_SUFFIXES)


def _is_conftest(path: str) -> bool:
    return Path(path).name == "conftest.py"


def is_migration(path: str) -> bool:
    return "/migrations/" in path or Path(path).name == "max_migration.txt"


def _is_astgrep_rule(path: str) -> bool:
    return path.startswith(".ast-grep/")


def _is_manifest(path: str) -> bool:
    return path in _MANIFEST_FILES


# Ordered FULL-trigger predicates on a path (status is checked separately). The
# first match wins and supplies the plan's reason; anything below falls through
# to _scopable_disposition. Data-driven so the classifier stays flat, not branchy.
_FULL_PATH_TRIGGERS: tuple[tuple[Callable[[str], bool], str], ...] = (
    (_is_astgrep_rule, ".ast-grep rule/manifest changed — a stricter/new rule can flag an untouched file"),
    (_is_manifest, "regression manifest changed — a new blocking rule can newly-violate a clean file"),
    (_is_conftest, "conftest changes doctest/fixture semantics tree-wide"),
    (is_migration, "migration/schema change affects the whole DB-test surface"),
    (_is_config, "toolchain/config is un-modellable"),
)


def _scopable_disposition(path: str) -> tuple[str, str]:
    """Classify a path that tripped no FULL trigger into ``src`` / ``tests`` / ``full`` / ``ignore``.

    ``ignore`` is reserved for the one provably-irrelevant class: markdown outside
    the code roots (never a src doctest input, never a python ast-grep target).
    Everything not positively recognised as scopable or irrelevant is ``full``.
    """
    under_src = path.startswith(_SRC_ROOT)
    under_tests = path.startswith(_TESTS_ROOT)
    if _is_python(path):
        if under_src:
            return "src", ""
        if under_tests:
            return "tests", ""
        return "full", f"python file outside the modelled src/tests roots ({path}) — ast-grep scans scripts/hooks too"
    if under_src or under_tests:
        return "full", f"non-python data file under a code dir ({path}) — data-driven tests read it at runtime"
    if path.endswith(_DOC_SUFFIXES):
        return "ignore", ""
    return "full", f"unclassifiable path — FULL (fail-safe): {path}"


def _path_disposition(entry: ChangeEntry) -> tuple[str, str]:
    """Classify one entry into ``full`` / ``src`` / ``tests`` / ``ignore`` + a reason."""
    path = entry.path
    if entry.status in _DESTRUCTIVE_STATUS:
        return "full", f"delete/rename/type-change ({entry.status} {path}) — edges on the current tree are stale"
    for predicate, reason in _FULL_PATH_TRIGGERS:
        if predicate(path):
            return "full", f"{reason} ({path})"
    return _scopable_disposition(path)


def classify(changed: ChangedSet) -> FullTrigger:
    """Route *changed* to a whole-tree FULL run, or the scoped src + test file lists.

    Any single path that trips a FULL trigger forces the whole-tree run (the reason
    is the first such trigger, in table order). Otherwise the verdict is scoped:
    ``scoped_src`` (changed ``src/teatree/**/*.py``, the doctest + ast-grep targets)
    and ``scoped_tests`` (changed ``tests/**/*.py``, ast-grep targets).
    """
    scoped_src: list[Path] = []
    scoped_tests: list[Path] = []
    for entry in changed.entries:
        kind, reason = _path_disposition(entry)
        if kind == "full":
            return FullTrigger(full=True, reason=reason)
        if kind == "src":
            scoped_src.append(Path(entry.path))
        elif kind == "tests":
            scoped_tests.append(Path(entry.path))
    return FullTrigger(
        full=False,
        reason="scoped to the diff — no FULL trigger",
        scoped_src=tuple(sorted(set(scoped_src))),
        scoped_tests=tuple(sorted(set(scoped_tests))),
    )


def _git(root: Path, *args: str) -> str:
    result = run_allowed_to_fail(["git", *args], expected_codes=None, cwd=root)
    if result.returncode != 0:
        message = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise ChangedSetError(message)
    return result.stdout


def _parse_name_status(output: str, entries: set[ChangeEntry]) -> None:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        fields = line.split("\t")
        # Rename/copy carry ``<old>\t<new>``; the new path is the one on the tree.
        entries.add(ChangeEntry(status=fields[0][:1], path=fields[-1]))


def changed_paths(base_ref: str = "origin/main", *, cwd: Path | None = None) -> ChangedSet:
    """Union the merge-base diff + staged + unstaged + untracked, keeping D/R status.

    Raises :class:`ChangedSetError` when the merge-base cannot be resolved (a dirty
    or shallow clone) — the caller treats that as a FULL trigger (R7): a gate that
    cannot compute its selection must run the whole tree, never skip-as-pass.
    """
    root = Path(cwd) if cwd is not None else Path.cwd()
    merge_base = _git(root, "merge-base", base_ref, "HEAD").strip()
    if not merge_base:
        message = f"empty merge-base for {base_ref}"
        raise ChangedSetError(message)
    entries: set[ChangeEntry] = set()
    _parse_name_status(_git(root, "diff", "--name-status", merge_base, "HEAD"), entries)
    _parse_name_status(_git(root, "diff", "--name-status", "--cached"), entries)
    _parse_name_status(_git(root, "diff", "--name-status"), entries)
    entries.update(
        ChangeEntry(status="A", path=line.strip())
        for line in _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
        if line.strip()
    )
    return ChangedSet(entries=tuple(sorted(entries, key=lambda e: (e.path, e.status))), base_ref=base_ref)
