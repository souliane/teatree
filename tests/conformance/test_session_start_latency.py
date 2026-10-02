# test-path: cross-cutting — runs the registered SessionStart chain; no single-module mirror.
"""SessionStart must answer well inside the SDK's initialize-handshake deadline.

``tests/test_hooks_json_declare_timeouts.py`` pins the ``timeout`` each hook
DECLARES. That is a statement about configuration, and configuration is not what
broke: ``bootstrap-cli.sh`` could have declared a bound and still consumed all of
it on every session. A hook that declares 10s and spends 9.9s passes that lane
forever. This lane measures what the chain actually COSTS.

The deadline is not a taste. The headless runner drives a
``claude_agent_sdk`` session; the SDK spawns a ``claude`` CLI child and blocks on
a control request until the child answers ``initialize``. That child runs these
hooks first, so a slow SessionStart is spent inside the handshake window. When
the window closes the SDK raises, the whole run dies with a raw traceback, and
:func:`teatree.core.modelkit.task_failure_taxonomy.classify_failure` records it
as an ENVIRONMENTAL ``harness_crash`` — a name that reads as transient, which is
how a factory completing nothing ran unnoticed for days. Measured on the
deployed box before the fix: 325s for one SessionStart hook against a 60s
deadline.

Two properties, deliberately separate:

*   **The chain is fast** — each hook's floor wall clock, and their sum, stay
    under a small fraction of the SDK's own deadline (read from the installed SDK
    rather than copied, so a change upstream cannot leave this lane asserting a
    number that no longer exists).
*   **The chain does not BLOCK ON ``t3``** — every measurement runs with a
    deliberately slow ``t3`` first on ``PATH``. ``t3`` is containerized and its
    cost is unbounded and not teatree's to control, so a SessionStart hook may
    never wait on it. This is what makes the lane catch the ORIGINAL defect
    rather than merely the machine it ran on: the old hook is slow here even on
    an idle runner, because the slowness is supplied by the stub.

What the baseline does NOT prove: ``run-hook.sh`` prefers a Django-capable
interpreter and falls back to the version floor alone, so on a runner with no
teatree venv the router measures its Django-free path — a cheaper chain than a
developer box runs. That weakens the BASELINE only. The catch does not depend on
it: the slow ``t3`` is supplied by this lane, so a hook that waits on the CLI
breaches on every venue, idle or loaded, Django or not.

Stability: the verdict is the MINIMUM of :data:`_RUNS`, not a mean or a median.
That is the right estimator for the question actually being asked. A regression
here is a call ADDED to the hook, so it is paid on every run and the minimum
carries it (the vacuity proof below measured 25.00s three times out of three);
contention on a shared runner is transient, so the minimum rejects it. The lane
runs under ``-n auto`` beside fifteen sibling shards, which is precisely the
noise a mean would import. If it ever does flake, the lever is more samples, NOT
a wider budget — the budget is pinned to the SDK's deadline and cannot be
loosened without the assertion ceasing to mean what it says.
"""

import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conformance._src_tree import REPO_ROOT

_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
_EVENT = "SessionStart"

#: The share of the SDK handshake deadline the whole chain may claim. The CLI child
#: also boots, loads plugins and starts MCP servers before it can answer, so the
#: hooks get a third and the rest of the handshake keeps two.
_DEADLINE_SHARE = 3

#: Wall-clock ceilings, in seconds. Both are derived from the deadline above and
#: checked against it by :func:`test_the_budgets_are_a_fraction_of_the_sdk_deadline`.
_CHAIN_BUDGET_S = 20.0
_PER_HOOK_BUDGET_S = 15.0

#: Samples per hook. The verdict is their minimum, so more samples only sharpen the
#: rejection of runner contention; three is the floor at which that is meaningful.
_RUNS = 3

#: How long the stub ``t3`` blocks. Past the per-hook budget by a clear margin, so a
#: hook that waits on it fails on the budget rather than on timing luck.
_SLOW_T3_SECONDS = _PER_HOOK_BUDGET_S * 2

#: Slack over the budget before the run is abandoned, so a genuinely wedged hook
#: fails the lane in bounded time instead of sitting on the package ceiling.
_RUN_TIMEOUT_S = _PER_HOOK_BUDGET_S + 10.0


@dataclass(frozen=True, slots=True)
class HookTiming:
    """One registered hook's measured cost across :data:`_RUNS` runs."""

    label: str
    samples: tuple[float, ...]

    @property
    def floor(self) -> float:
        """The least-contended sample — this hook's cost with the runner's noise removed."""
        return min(self.samples)

    def __str__(self) -> str:
        spread = ", ".join(f"{sample:.2f}s" for sample in self.samples)
        return f"{self.label}: {self.floor:.2f}s (runs: {spread})"


def sdk_initialize_deadline_s() -> float:
    """The installed SDK's own initialize-request deadline, read rather than copied.

    ``claude_agent_sdk`` hard-codes the value as a parameter default instead of
    exporting a constant, so the signature IS the source. A rename upstream fails
    loudly here — which is correct: the budget below is derived from this number and
    has to be re-derived if it moves.
    """
    import inspect  # noqa: PLC0415 — deferred: only this lane introspects the SDK

    from claude_agent_sdk._internal.query import Query  # noqa: PLC0415 — deferred: same

    parameter = inspect.signature(Query.__init__).parameters.get("initialize_timeout")
    if parameter is None or not isinstance(parameter.default, int | float):
        pytest.fail(
            "claude_agent_sdk no longer exposes an 'initialize_timeout' default on Query.__init__ — "
            "the SessionStart budget in this module was derived from it and must be re-derived",
        )
    return float(parameter.default)


def session_start_commands() -> list[tuple[str, list[str]]]:
    """Every ``SessionStart`` registration as ``(label, argv)``, in registered order."""
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    commands: list[tuple[str, list[str]]] = []
    for matcher in config["hooks"].get(_EVENT, []):
        for hook in matcher.get("hooks", []):
            command = hook.get("command", "").replace("${CLAUDE_PLUGIN_ROOT}", str(REPO_ROOT))
            argv = shlex.split(command)
            commands.append((" ".join(Path(part).name for part in argv if part.endswith((".sh", ".py"))), argv))
    return commands


def _slow_t3(directory: Path) -> Path:
    """A ``t3`` that blocks past the budget, so waiting on the CLI is always a failure."""
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "t3"
    stub.write_text(f"#!/bin/sh\nsleep {_SLOW_T3_SECONDS:.0f}\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _hook_env(*, stub_dir: Path, state_dir: Path) -> dict[str, str]:
    """The ambient environment, with the slow ``t3`` in front and every side effect diverted.

    Ambient rather than hermetic on purpose: ``run-hook.sh`` picks its interpreter from
    ``HOME``/``UV_TOOL_DIR``, so a scrubbed environment would measure a hook chain that
    never found a Django-capable Python — the fast, inert path, not the real one.
    """
    return os.environ | {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state_dir),
        # Measuring the enabled arm would spawn a real detached worker from a test run.
        "T3_LOOP_RUNNER_ENABLED": "0",
    }


def _measure(argv: list[str], env: dict[str, str]) -> float:
    """Wall clock for one hook invocation; an abandoned run reports its own ceiling."""
    payload = json.dumps(
        {
            "session_id": f"latency-{uuid.uuid4()}",
            "hook_event_name": _EVENT,
            "source": "startup",
            "cwd": str(REPO_ROOT),
            "transcript_path": "/dev/null",
        },
    ).encode()
    started = time.perf_counter()
    try:
        subprocess.run(
            argv,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=REPO_ROOT,
            env=env,
            timeout=_RUN_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _RUN_TIMEOUT_S
    return time.perf_counter() - started


@pytest.fixture(scope="module")
def timings(tmp_path_factory: pytest.TempPathFactory) -> tuple[HookTiming, ...]:
    """Measure the registered chain once, and share the breakdown with every lane here."""
    root = tmp_path_factory.mktemp("session-start-latency")
    stub_dir = root / "bin"
    _slow_t3(stub_dir)
    env = _hook_env(stub_dir=stub_dir, state_dir=root / "statusline")
    return tuple(
        HookTiming(label=label, samples=tuple(_measure(argv, env) for _ in range(_RUNS)))
        for label, argv in session_start_commands()
    )


def test_the_budgets_are_a_fraction_of_the_sdk_deadline() -> None:
    deadline = sdk_initialize_deadline_s()
    assert deadline / _DEADLINE_SHARE >= _CHAIN_BUDGET_S, (
        f"the SessionStart chain budget ({_CHAIN_BUDGET_S}s) claims more than 1/{_DEADLINE_SHARE} of the SDK's "
        f"{deadline}s initialize deadline, leaving too little for the CLI child's own boot"
    )
    assert _CHAIN_BUDGET_S >= _PER_HOOK_BUDGET_S


def test_the_registered_chain_is_actually_measured(timings: tuple[HookTiming, ...]) -> None:
    """A budget nothing runs against is not a budget — pin that the chain is non-empty."""
    assert timings, f"no {_EVENT} registrations found in {_HOOKS_JSON} — the budgets below prove nothing"


def _breakdown(timings: tuple[HookTiming, ...]) -> str:
    """Every hook's cost, so a breach names its culprit instead of starting a hunt."""
    return "\n  ".join(str(timing) for timing in timings)


def test_no_session_start_hook_blocks_past_its_budget(timings: tuple[HookTiming, ...]) -> None:
    breach = tuple(timing for timing in timings if timing.floor > _PER_HOOK_BUDGET_S)
    assert not breach, (
        f"these {_EVENT} hooks exceed the {_PER_HOOK_BUDGET_S}s per-hook budget and will eat the SDK's "
        f"initialize handshake:\n  {_breakdown(breach)}\nfull chain:\n  {_breakdown(timings)}"
    )


def test_the_whole_session_start_chain_fits_its_budget(timings: tuple[HookTiming, ...]) -> None:
    total = sum(timing.floor for timing in timings)
    assert total <= _CHAIN_BUDGET_S, (
        f"the {_EVENT} chain costs {total:.2f}s against a {_CHAIN_BUDGET_S}s budget:\n  {_breakdown(timings)}"
    )
