"""Drive mutmut over the diff-scoped subset of the high-value safety registry.

The cheap gate (diff ∩ registry) lives in :mod:`teatree.quality.mutation`. This
module is the runner that, when the intersection is non-empty, writes a scoped
mutmut config and executes mutmut over ONLY the touched safety modules, then
classifies the result and applies the warn/block ratchet.

Two design choices keep it robust and narrow. Serial (debug) execution:
mutmut's default forks a child per mutant; on macOS a forked child that has
already imported pytest segfaults on exit, reporting every mutant as segfault.
``debug = true`` runs serially and is deterministic across macOS and Linux —
fine for the handful of small modules in scope. Per-module ``tests_dir``:
mutmut's baseline "clean tests" pass runs the whole ``tests_dir`` once; scoping
it per module (from ``[tool.teatree.mutation.module_tests]``) keeps each run
inside the CI cap.

A mutant is a *survivor* only when its status is ``survived`` or ``no tests`` —
the cases where the test suite did not catch the change. ``timeout`` /
``segfault`` / ``suspicious`` are *inconclusive* (an environment artifact, not a
test gap) and never fail the gate.
"""

import dataclasses
import re
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

from teatree.quality.mutation import (
    MutationConfigError,
    load_high_value_modules,
    registry_pyproject_path,
    scope_modules,
)
from teatree.utils import git
from teatree.utils.run import run_allowed_to_fail

_MODES = frozenset({"warn", "block"})
# mutmut result statuses that mean the suite did NOT catch the mutant.
_SURVIVOR_STATUSES = frozenset({"survived", "no tests"})
_KILLED_STATUSES = frozenset({"killed", "caught by type check", "skipped"})
_INCONCLUSIVE_STATUSES = frozenset(
    {"timeout", "segfault", "suspicious", "not checked", "check was interrupted by user"},
)
_ALL_STATUSES = _SURVIVOR_STATUSES | _KILLED_STATUSES | _INCONCLUSIVE_STATUSES
# A mutmut ``results`` line is ``    <mutant-name>: <status>``. Anchor on the
# known statuses so header/spinner lines never parse as a result.
_RESULT_LINE_RE = re.compile(
    rf"^\s*(?P<name>\S.*?): (?P<status>{'|'.join(re.escape(s) for s in sorted(_ALL_STATUSES))})\s*$",
)


@dataclasses.dataclass(frozen=True)
class MutationSettings:
    mode: str
    timeout_seconds: int
    module_tests: dict[str, tuple[str, ...]]
    baseline_total: int


@dataclasses.dataclass(frozen=True)
class MutationResult:
    killed: tuple[str, ...]
    survived: tuple[str, ...]
    inconclusive: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class MutationOutcome:
    scoped_modules: tuple[str, ...]
    survived: tuple[str, ...]
    killed: tuple[str, ...]
    inconclusive: tuple[str, ...]

    @property
    def is_no_op(self) -> bool:
        return not self.scoped_modules


def load_settings(pyproject_path: Path | None = None) -> MutationSettings:
    path = pyproject_path or registry_pyproject_path()
    section = tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {}).get("teatree", {}).get("mutation", {})
    mode = section.get("mode", "warn")
    if mode not in _MODES:
        detail = f"mode must be one of {sorted(_MODES)}, got {mode!r}"
        raise MutationConfigError(detail)
    raw_tests = section.get("module_tests", {})
    module_tests = {module: tuple(dirs) for module, dirs in raw_tests.items()}
    baseline = sum(int(entry.get("count", 0)) for entry in section.get("baseline_surviving", []))
    return MutationSettings(
        mode=mode,
        timeout_seconds=int(section.get("timeout_seconds", 540)),
        module_tests=module_tests,
        baseline_total=baseline,
    )


def load_baseline_per_module(pyproject_path: Path | None = None) -> dict[str, int]:
    """The committed per-module surviving-mutant baseline (``path`` → ``count``).

    Reads the ``baseline_surviving`` array of ``{ path, count }`` tables. A module
    absent from the array has a baseline of zero (no recorded survivors).
    """
    path = pyproject_path or registry_pyproject_path()
    section = tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {}).get("teatree", {}).get("mutation", {})
    return {str(entry["path"]): int(entry.get("count", 0)) for entry in section.get("baseline_surviving", [])}


def tests_for(module: str, settings: MutationSettings) -> tuple[str, ...]:
    return settings.module_tests.get(module, settings.module_tests.get("default", ("tests/",)))


def build_mutmut_config(modules: Sequence[str], *, tests_dir: Sequence[str]) -> str:
    """Render the ``[mutmut]`` ``setup.cfg`` section scoping the run.

    mutmut reads ``[tool.mutmut]`` from ``pyproject.toml`` first; with no such
    key it falls back to ``setup.cfg`` ``[mutmut]``. teatree's pyproject carries
    ``[tool.teatree.mutation]`` (a different key), so a generated ``setup.cfg``
    drives the scoped run without touching the real config. Multi-value options
    are newline-separated, the ini-list shape mutmut parses.
    """

    def _ini_list(values: Sequence[str]) -> str:
        return "\n    " + "\n    ".join(values)

    # ``also_copy`` mirrors the whole package into the ``mutants/`` tree so a
    # scoped module's test can still import its siblings (``teatree.core`` etc.).
    # mutmut only copies ``paths_to_mutate`` by default, which breaks any test
    # that imports beyond the one module. ``tests/`` + ``conftest.py`` +
    # ``pyproject.toml`` are appended by mutmut itself. Only directories — a
    # bare-file ``also_copy`` entry skips creating its parent dir and crashes.
    also_copy = ["src/"]
    # Plugins disabled in the mutant child: ``cacheprovider`` (stale .pytest_cache),
    # ``cov``/``doctest`` (project addopts), and the signal/thread/process plugins
    # (``timeout``, ``xdist``, ``tach``) whose handlers segfault a fork()ed child
    # after Django setup.
    pytest_args = [
        "-p",
        "no:cacheprovider",
        "--no-cov",
        "-p",
        "no:doctest",
        "-p",
        "no:timeout",
        "-p",
        "no:xdist",
        "-p",
        "no:tach",
    ]
    return (
        "[mutmut]\n"
        f"paths_to_mutate ={_ini_list(modules)}\n"
        f"tests_dir ={_ini_list(tests_dir)}\n"
        f"also_copy ={_ini_list(also_copy)}\n"
        "debug = true\n"
        f"pytest_add_cli_args ={_ini_list(pytest_args)}\n"
    )


def parse_results(raw: str) -> MutationResult:
    killed: list[str] = []
    survived: list[str] = []
    inconclusive: list[str] = []
    for line in raw.splitlines():
        match = _RESULT_LINE_RE.match(line)
        if not match:
            continue
        name, status = match.group("name"), match.group("status")
        if status in _SURVIVOR_STATUSES:
            survived.append(name)
        elif status in _KILLED_STATUSES:
            killed.append(name)
        else:
            inconclusive.append(name)
    return MutationResult(killed=tuple(killed), survived=tuple(survived), inconclusive=tuple(inconclusive))


class BaselineRatchet:
    """The programmatic surviving-mutant ratchet over a :class:`MutationOutcome`.

    The surviving count may only ever shrink. A run above the recorded baseline
    is a regression CI must catch; a run below it auto-tightens the baseline. All
    decisions are pure functions of an outcome plus the committed baseline, so
    they live together here rather than scattered as module-level functions.
    """

    @staticmethod
    def module_dotted_prefix(module: str) -> str:
        """The importable dotted prefix of a registry path (``src/teatree/x.py`` → ``teatree.x``).

        mutmut names a mutant ``<dotted-module>.<func>__mutmut_<n>``, so the
        dotted form of each registry path attributes a survivor to its module.
        """
        return module.removeprefix("src/").removesuffix(".py").replace("/", ".")

    @classmethod
    def survivors_per_module(cls, outcome: MutationOutcome) -> dict[str, int]:
        """Count surviving mutants attributed to each scoped registry module.

        A survivor's mutmut name starts with its module's dotted prefix. Longest
        matching prefix wins so a nested module (``teatree.core.merge.execution``)
        is not stolen by a shorter sibling. A survivor matching no scoped module
        is not attributed (it cannot, by construction — mutmut only mutates the
        scoped paths), so the counts sum to at most ``len(outcome.survived)``.
        """
        prefixes = sorted(
            ((cls.module_dotted_prefix(m), m) for m in outcome.scoped_modules),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
        counts = dict.fromkeys(outcome.scoped_modules, 0)
        for name in outcome.survived:
            for prefix, module in prefixes:
                if name == prefix or name.startswith(f"{prefix}."):
                    counts[module] += 1
                    break
        return counts

    @staticmethod
    def exceeds_baseline(outcome: MutationOutcome, *, baseline: int) -> bool:
        """True when the run surfaced MORE surviving mutants than the recorded baseline.

        A run above the baseline is a regression that fails CI regardless of
        ``mode`` — what makes ``mode = "block"`` safe to flip later. A no-op run
        (no safety module in the diff) surfaces nothing and never exceeds.
        """
        if outcome.is_no_op:
            return False
        return len(outcome.survived) > baseline

    @classmethod
    def per_module(
        cls,
        outcome: MutationOutcome,
        *,
        committed: dict[str, int],
    ) -> tuple[dict[str, int], bool]:
        """The new per-module baseline (only shrinks) and whether the diff would loosen.

        For every scoped module the new count is ``min(committed, measured)`` — a
        module with fewer survivors auto-tightens, one at-or-above holds its lower
        committed count. ``committed`` entries for modules NOT in this run are
        carried through unchanged (a diff-scoped run only observed a subset). The
        second element is True when any scoped module measured MORE survivors than
        its committed baseline — the regression the rewrite refuses without an
        explicit override (mirrors test_shape's ``loosens_baseline``).
        """
        measured = cls.survivors_per_module(outcome)
        loosens = any(count > committed.get(module, 0) for module, count in measured.items())
        new_baseline = dict(committed)
        for module, count in measured.items():
            new_baseline[module] = min(committed.get(module, count), count)
        return new_baseline, loosens

    @classmethod
    def verdict(cls, outcome: MutationOutcome, *, mode: str, baseline: int) -> int:
        """Exit code for ``t3 mutation run`` — the surviving-count ratchet.

        A run above the recorded baseline fails (exit 1) in BOTH ``warn`` and
        ``block`` mode: the surviving count may only ever shrink, so a PR that
        surfaces more survivors than the baseline is a regression CI must catch.
        This is the prerequisite that makes flipping ``mode`` to ``"block"`` safe
        — ``mode`` stays as the lever for that follow-up (where it will gate on
        survivors existing at all); today both modes coincide on the ratchet.
        """
        if mode not in _MODES:
            detail = f"mode must be one of {sorted(_MODES)}, got {mode!r}"
            raise MutationConfigError(detail)
        return 1 if cls.exceeds_baseline(outcome, baseline=baseline) else 0


def changed_files_vs_main(repo: str = ".", target: str = "origin/main") -> tuple[str, ...]:
    base = git.merge_base(repo=repo, target=target)
    out = git.run(repo=repo, args=["diff", "--name-only", f"{base}...HEAD"])
    return tuple(line for line in out.splitlines() if line.strip())


def run_scoped(
    *,
    target: str = "origin/main",
    all_modules: bool = False,
    changed_files: Iterable[str] | None = None,
    settings: MutationSettings | None = None,
    registry: Sequence[str] | None = None,
) -> MutationOutcome:
    settings = settings or load_settings()
    registry = tuple(registry) if registry is not None else load_high_value_modules()
    if all_modules:
        scoped = registry
    else:
        changed = tuple(changed_files) if changed_files is not None else changed_files_vs_main(target=target)
        scoped = scope_modules(changed, registry=registry)
    if not scoped:
        return MutationOutcome(scoped_modules=(), survived=(), killed=(), inconclusive=())

    tests_dir: list[str] = []
    for module in scoped:
        for path in tests_for(module, settings):
            if path not in tests_dir:
                tests_dir.append(path)

    result = _run_mutmut(scoped, tests_dir=tuple(tests_dir), repo=".", timeout=settings.timeout_seconds)
    return MutationOutcome(
        scoped_modules=scoped,
        survived=result.survived,
        killed=result.killed,
        inconclusive=result.inconclusive,
    )


_MUTMUT_CMD = ("uv", "run", "--group", "mutation", "mutmut")


def _mutmut_env() -> dict[str, str]:
    import os  # noqa: PLC0415

    env = dict(os.environ)
    # macOS aborts a fork()ed child that touches the Objective-C runtime once a
    # parent thread has initialized it (mutmut starts a timeout thread before
    # forking). Disabling the fork-safety check lets the child run the test;
    # the var is a no-op on Linux CI.
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    return env


# Neutralizes the project's heavy pytest addopts (``--doctest-modules``,
# ``--cov``, ``--failed-first``) inside the ``mutants/`` copy. ``pytest.ini``
# outranks ``pyproject.toml`` in pytest's config discovery, and mutmut copies
# both into the mutants tree, so this wins.
_MUTANTS_PYTEST_INI = "[pytest]\naddopts =\nDJANGO_SETTINGS_MODULE = tests.django_settings\n"


def _run_mutmut(modules: Sequence[str], *, tests_dir: Sequence[str], repo: str, timeout: int) -> MutationResult:
    config_path = Path(repo) / "setup.cfg"
    pytest_ini = Path(repo) / "pytest.ini"
    for path in (config_path, pytest_ini):
        if path.exists():
            msg = f"{path} already exists; refusing to overwrite the scoped mutation config"
            raise FileExistsError(msg)
    config_path.write_text(build_mutmut_config(modules, tests_dir=tests_dir), encoding="utf-8")
    pytest_ini.write_text(_MUTANTS_PYTEST_INI, encoding="utf-8")
    env = _mutmut_env()
    try:
        run_allowed_to_fail([*_MUTMUT_CMD, "run"], expected_codes=None, cwd=repo, env=env, timeout=timeout)
        # ``results --all=1`` lists killed mutants too — without it mutmut hides
        # them, so the kill-proof could not observe a kill. ``--all`` is a
        # value option in mutmut, not a flag, so it needs ``=1``.
        results = run_allowed_to_fail(
            [*_MUTMUT_CMD, "results", "--all=1"],
            expected_codes=None,
            cwd=repo,
            env=env,
            timeout=60,
        )
    finally:
        config_path.unlink(missing_ok=True)
        pytest_ini.unlink(missing_ok=True)
    return parse_results(results.stdout)
