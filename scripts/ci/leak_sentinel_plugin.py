"""Pytest plugin: fail the POLLUTER that leaks process-global state, not its victim.

An order-dependent shard/shuffle red ("green locally, red in shard 3") is almost
always a test that mutates a process-global surface and never restores it: a later
VICTIM test then fails because it inherited the dirty state. The traceback points at
the victim, so the polluter hides and the failure is non-deterministic.

This plugin snapshots the known process-global surface — ``os.environ`` and the cwd,
the two ``conftest.py``'s ``monkeypatch``-managed fixtures do NOT auto-revert when a
test mutates them DIRECTLY (``os.environ[k] = v`` / ``os.chdir(...)`` instead of the
``monkeypatch`` seam) — BEFORE each test's fixtures set up and AFTER they fully tear
down (so ``monkeypatch``'s own reversions are already applied). A mismatch is attributed
to the test that produced it — the POLLUTER — turning a non-deterministic downstream
red into a named, deterministic local failure.

Loaded opt-in via ``-p scripts.ci.leak_sentinel_plugin --leak-sentinel=<mode>``:

- ``off`` (default) — the plugin registers nothing; zero overhead on a normal run.
- ``warn`` — record every leak and print a terminal summary naming each polluter,
    never failing the run (the rollout mode: surface polluters without breaking CI).
- ``error`` — additionally fail (error at teardown) the polluting test, so a leak
    reds the lane deterministically.
"""

import dataclasses
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

    from _pytest.terminal import TerminalReporter

_OPTION = "--leak-sentinel"
_MODE_OFF = "off"
_MODE_WARN = "warn"
_MODE_ERROR = "error"
_MODES = (_MODE_OFF, _MODE_WARN, _MODE_ERROR)

#: Env vars pytest/coverage mutate per-test; a change here is never a real leak.
_VOLATILE_ENV_KEYS: frozenset[str] = frozenset({"PYTEST_CURRENT_TEST"})


@dataclasses.dataclass(frozen=True)
class Snapshot:
    env: dict[str, str]
    cwd: str

    @classmethod
    def capture(cls) -> "Snapshot":
        try:
            cwd = str(Path.cwd())
        except OSError:
            # A test that left cwd in a since-deleted dir is itself a leak; record the
            # unavailability rather than crashing so the diff still names the polluter.
            cwd = "<unavailable>"
        return cls(env=dict(os.environ), cwd=cwd)


@dataclasses.dataclass(frozen=True)
class LeakDiff:
    env_added: tuple[str, ...]
    env_removed: tuple[str, ...]
    env_changed: tuple[str, ...]
    cwd_from: str | None
    cwd_to: str | None

    @property
    def is_empty(self) -> bool:
        return not (self.env_added or self.env_removed or self.env_changed or self.cwd_from is not None)

    def describe(self) -> str:
        parts: list[str] = []
        if self.env_added:
            parts.append(f"env added {list(self.env_added)}")
        if self.env_removed:
            parts.append(f"env removed {list(self.env_removed)}")
        if self.env_changed:
            parts.append(f"env changed {list(self.env_changed)}")
        if self.cwd_from is not None:
            parts.append(f"cwd {self.cwd_from!r} -> {self.cwd_to!r}")
        return "; ".join(parts)


def diff_snapshots(
    before: Snapshot,
    after: Snapshot,
    *,
    ignore: frozenset[str] = _VOLATILE_ENV_KEYS,
) -> LeakDiff:
    """The process-global surface *after* left dirty relative to *before* (ignoring volatile keys)."""
    before_env = {k: v for k, v in before.env.items() if k not in ignore}
    after_env = {k: v for k, v in after.env.items() if k not in ignore}
    added = tuple(sorted(set(after_env) - set(before_env)))
    removed = tuple(sorted(set(before_env) - set(after_env)))
    changed = tuple(sorted(k for k in set(before_env) & set(after_env) if before_env[k] != after_env[k]))
    cwd_changed = before.cwd != after.cwd
    return LeakDiff(
        env_added=added,
        env_removed=removed,
        env_changed=changed,
        cwd_from=before.cwd if cwd_changed else None,
        cwd_to=after.cwd if cwd_changed else None,
    )


class LeakDetectedError(Exception):
    """Raised at teardown in ``error`` mode to fail the polluting test itself."""


_BEFORE_KEY: "pytest.StashKey[Snapshot]" = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        _OPTION,
        action="store",
        default=_MODE_OFF,
        choices=_MODES,
        help="Detect the test that leaks env/cwd process-global state: off (default), warn, or error.",
    )


def pytest_configure(config: pytest.Config) -> None:
    mode = config.getoption(_OPTION)
    if mode != _MODE_OFF:
        config.pluginmanager.register(LeakSentinelPlugin(mode), "leak-sentinel-plugin")


#: ``config.workeroutput`` key an xdist worker ships its serialised leaks under.
_WORKEROUTPUT_KEY = "leak_sentinel_leaks"


class LeakSentinelPlugin:
    def __init__(self, mode: str) -> None:
        self._mode = mode
        #: Leaks detected in THIS process (populated at teardown). On an xdist
        #: worker this is the worker-local set; on a serial run it is the whole set.
        self.leaks: list[tuple[str, LeakDiff]] = []
        #: Leaks fanned IN from finished xdist workers (controller-side only). A
        #: worker has no terminal, so its ``pytest_terminal_summary`` never fires;
        #: without this fan-in warn-mode names no polluter under xdist (CI-1).
        self._worker_leaks: list[tuple[str, str]] = []

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> "Generator[None, object]":  # noqa: PLR6301 — pytest invokes this hookimpl on the registered plugin INSTANCE; it must stay a method
        # Pre-yield: BEFORE this item's fixtures set up, so the baseline predates any
        # monkeypatch-managed env/cwd the fixtures install (they revert symmetrically).
        item.stash[_BEFORE_KEY] = Snapshot.capture()
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> "Generator[None, object]":
        yield  # run the actual teardown (monkeypatch.undo + fixture finalizers) FIRST
        before = item.stash.get(_BEFORE_KEY, None)
        if before is None:
            return
        diff = diff_snapshots(before, Snapshot.capture())
        if diff.is_empty:
            return
        self.leaks.append((item.nodeid, diff))
        if self._mode == _MODE_ERROR:
            message = (
                f"{item.nodeid} leaked process-global state ({diff.describe()}). "
                "Restore any env/cwd it mutates — prefer the monkeypatch fixture, which reverts for you."
            )
            raise LeakDetectedError(message)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        """On an xdist worker, ship this worker's leaks to the controller (CI-1).

        A worker has no terminal, so :meth:`pytest_terminal_summary` never fires on
        it and its ``self.leaks`` would be lost — the warn-mode silent no-op under
        xdist. xdist forwards ``config.workeroutput`` back to the controller (it
        collects it in :meth:`pytest_testnodedown`); ``workeroutput`` exists ONLY on
        a worker, so a serial / controller run skips this. Serialise to plain
        ``(nodeid, description)`` string pairs — execnet marshals only basic types,
        not the :class:`LeakDiff` dataclass.
        """
        workeroutput = getattr(session.config, "workeroutput", None)
        if workeroutput is None:
            return
        workeroutput[_WORKEROUTPUT_KEY] = [(nodeid, diff.describe()) for nodeid, diff in self.leaks]

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: object, error: object) -> None:  # noqa: ARG002 — xdist hookspec signature
        """On the controller, collect a finished xdist worker's leak findings (CI-1).

        Each worker ships its serialised leaks in ``node.workeroutput``; the
        controller accumulates them so :meth:`pytest_terminal_summary` names every
        polluter under xdist exactly as it does on a serial run. ``optionalhook``
        keeps this a no-op when xdist is not installed (the hookspec is absent).
        """
        workeroutput = getattr(node, "workeroutput", None)
        if not isinstance(workeroutput, dict):
            return
        for entry in workeroutput.get(_WORKEROUTPUT_KEY, ()):
            nodeid, description = entry
            self._worker_leaks.append((nodeid, description))

    def pytest_terminal_summary(self, terminalreporter: "TerminalReporter") -> None:
        findings: list[tuple[str, str]] = [(nodeid, diff.describe()) for nodeid, diff in self.leaks]
        findings.extend(self._worker_leaks)
        if not findings:
            return
        terminalreporter.section(f"leak sentinel ({self._mode})")
        for nodeid, description in findings:
            terminalreporter.write_line(f"POLLUTER {nodeid}: {description}")
