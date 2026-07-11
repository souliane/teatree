"""Hooks must run under an interpreter new enough for the hook modules.

Regression guard for the bootstrap crash introduced by b7c0d0df89 (#2559/#2571).
``availability_away_probe.py`` declared ``def _availability_show(...) -> str | None``
— a PEP-604 union evaluated at *import* time (return annotations evaluate at
def-time) — and ``hook_router.py`` imports it at module top. The project baseline
is Python >= 3.13 and standardizes on native ``X | Y`` unions (ruff bans
``from __future__ import annotations`` via TID251), so the union itself is
correct. The bug was the *interpreter*: ``hooks.json`` invoked the router with a
bare ``python3``, which on some hosts (e.g. macOS system Python 3.9) is < 3.10,
where the native union raises ``TypeError`` at module load — taking down EVERY
hooked session at bootstrap.

The fix is not version-specific source rewrites (the future import is banned, and
``hook_router.py`` also imports 3.11+ ``tomllib`` via ``teatree_settings``, so it
can never import under < 3.11 anyway). The durable, project-aligned fix is to
invoke the hooks with a >= 3.11 interpreter: ``hooks.json`` routes the router
through the ``run-hook.sh`` selector, which picks the newest available >= 3.11
Python instead of trusting whatever bare ``python3`` resolves to.

These tests pin that fix end to end:

* :class:`TestHooksJsonPinsModernPython` — every router invocation routes through
    the selector, never a bare ``python3`` (anti-vacuous: reverting ``hooks.json``
    to ``python3 …`` turns it RED).
* :class:`TestRunHookSelectsModernPython` — the selector execs a >= 3.11
    interpreter, under which both ``hook_router`` and the reported
    ``availability_away_probe`` module import cleanly.
* :class:`TestInterpreterPinIsLoadBearing` — demonstrates WHY the pin is needed:
    the reported module genuinely fails to import under a < 3.11 interpreter (run
    when one is available; skipped on a 3.13-only CI runner).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "hooks" / "scripts"
_HOOKS_JSON = _REPO_ROOT / "hooks" / "hooks.json"
_RUN_HOOK = _SCRIPTS_DIR / "run-hook.sh"


def _router_commands() -> list[str]:
    """Every ``command`` string in hooks.json that invokes the hook router."""
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    commands: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            command = obj.get("command")
            if isinstance(command, str):
                commands.append(command)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(config)
    return [c for c in commands if "hook_router.py" in c]


def _env_with_executable_on_path() -> dict[str, str]:
    """Env whose PATH starts with this (>= 3.13) interpreter's dir.

    Makes the selector deterministically find a >= 3.11 candidate even on a host
    whose bare ``python3`` is older.
    """
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return env


def _import_under(interpreter: str, module: str) -> subprocess.CompletedProcess[str]:
    """Run ``import <module>`` under ``interpreter`` with hooks/scripts on sys.path."""
    return subprocess.run(
        [interpreter, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import " + module, str(_SCRIPTS_DIR)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _legacy_python() -> str | None:
    """A discoverable Python 3.9 / 3.10 interpreter, or None.

    These are the versions where the native PEP-604 union at module load crashes
    — the interpreters the pin protects against.
    """
    for candidate in ("python3.9", "python3.10", "/usr/bin/python3"):
        binary = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if not binary:
            continue
        probe = subprocess.run(
            [binary, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.stdout.strip() in {"3.9", "3.10"}:
            return binary
    return None


class TestHooksJsonPinsModernPython:
    """hooks.json invokes the router via the >= 3.11 selector, not a bare python3."""

    def test_router_is_always_invoked_through_the_selector(self) -> None:
        commands = _router_commands()
        assert commands, "expected hooks.json to invoke hook_router.py"
        for command in commands:
            assert "run-hook.sh" in command, f"router command must route through run-hook.sh, got: {command!r}"
            assert not command.lstrip().startswith("python3 "), (
                f"router command must not invoke a bare python3 (the bug), got: {command!r}"
            )

    def test_selector_script_exists_and_is_executable(self) -> None:
        assert _RUN_HOOK.is_file(), f"missing selector script {_RUN_HOOK}"
        assert os.access(_RUN_HOOK, os.X_OK), f"{_RUN_HOOK} must be executable"


class TestRunHookSelectsModernPython:
    """The selector execs a >= 3.11 interpreter the hook modules import under."""

    def test_wrapper_selects_python_ge_311(self) -> None:
        result = subprocess.run(
            [str(_RUN_HOOK), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            env=_env_with_executable_on_path(),
            timeout=30,
            check=False,
        )
        out = result.stdout.strip()
        assert out, f"selector produced no interpreter (no Python >= 3.11 on PATH); stderr={result.stderr!r}"
        major, minor = (int(part) for part in out.split("."))
        assert (major, minor) >= (3, 11), f"selector chose Python {out}, expected >= 3.11"

    def test_router_imports_under_selected_interpreter(self) -> None:
        # End-to-end: the whole router — including the line-49 import of
        # availability_away_probe (its line-74 native union) AND 3.11+ tomllib —
        # imports cleanly under the interpreter the selector picks.
        result = subprocess.run(
            [
                str(_RUN_HOOK),
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import hook_router",
                str(_SCRIPTS_DIR),
            ],
            capture_output=True,
            text=True,
            env=_env_with_executable_on_path(),
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, f"hook_router failed to import under the selector: {result.stderr.strip()}"

    def test_reported_module_imports_under_selected_interpreter(self) -> None:
        result = subprocess.run(
            [
                str(_RUN_HOOK),
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import availability_away_probe",
                str(_SCRIPTS_DIR),
            ],
            capture_output=True,
            text=True,
            env=_env_with_executable_on_path(),
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, (
            f"availability_away_probe failed to import under the selector: {result.stderr.strip()}"
        )


class TestInterpreterPinIsLoadBearing:
    """The reported module crashes under < 3.11 — which is why the pin exists."""

    def test_reported_module_crashes_under_legacy_python(self) -> None:
        legacy = _legacy_python()
        if legacy is None:
            pytest.skip("no Python 3.9/3.10 interpreter available to demonstrate the crash")
        result = _import_under(legacy, "availability_away_probe")
        assert result.returncode != 0, (
            f"expected availability_away_probe to fail importing under {legacy} (PEP-604 union "
            f"evaluated at module load on < 3.11); it imported cleanly, so the pin would be vacuous"
        )

    def test_reported_module_imports_under_a_modern_interpreter(self) -> None:
        # The contrast to the test above: under this (>= 3.13) interpreter — the
        # kind the selector picks — the same module imports without error.
        result = _import_under(sys.executable, "availability_away_probe")
        assert result.returncode == 0, (
            f"availability_away_probe should import under {sys.executable}: {result.stderr.strip()}"
        )

    def test_subagent_no_commit_sibling_cold_imports(self) -> None:
        # The extracted SubagentStop no-commit sibling (#2384 Wave-2 PR1) must
        # cold-import under a bare interpreter — its module-top state_files import
        # and dual-identity alias resolve with hooks/scripts on sys.path and no
        # Django, the way the live SubagentStop hook subprocess loads it.
        result = _import_under(sys.executable, "subagent_no_commit")
        assert result.returncode == 0, (
            f"subagent_no_commit should cold-import under {sys.executable}: {result.stderr.strip()}"
        )

    def test_banned_terms_gate_sibling_cold_imports(self) -> None:
        # The consolidated PreToolUse banned-terms publish gate (U17) must
        # cold-import under a bare interpreter — its module-top teatree_settings
        # / banned_terms.deny / banned_terms.marker package imports resolve with
        # no Django, the way the live PreToolUse hook subprocess loads it.
        result = _import_under(sys.executable, "hooks.scripts.banned_terms.gate")
        assert result.returncode == 0, (
            f"banned_terms.gate should cold-import under {sys.executable}: {result.stderr.strip()}"
        )
