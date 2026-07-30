"""Hooks must run under an interpreter new enough for the hook modules.

Regression guard for the bootstrap crash introduced by b7c0d0df89 (#2559/#2571).
The posture probe ``mode_posture_probe.py`` reached a PEP-604 union evaluated at
*import* time (return annotations evaluate at def-time — today via its
``managed_repo`` import), and ``hook_router.py`` imports it at module top. The project baseline
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
    ``mode_posture_probe`` module import cleanly.
* :class:`TestInterpreterPinIsLoadBearing` — demonstrates WHY the pin is needed:
    the reported module genuinely fails to import under a < 3.11 interpreter (run
    when one is available; skipped on a 3.13-only CI runner).
* :class:`TestRunHookPrefersDjangoCapableInterpreter` — the version floor is
    necessary but NOT sufficient. ``django_bootstrap.bootstrap_teatree_django``
    needs Django from the *interpreter*, and teatree installs into a uv-tool venv
    rather than the system python a bare ``python3`` resolves to. When it is
    missing every DB-backed handler silently no-ops — the SessionStart hand-off
    drain among them, which is how hand-offs accumulated unclaimed for a week.
    So the selector prefers an interpreter that can ``import django``, falling
    back to the version floor alone. Driven with stub interpreters so the
    selection logic is pinned without depending on what the host has installed.
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
_BASH = shutil.which("bash") or "/bin/bash"


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
        # mode_posture_probe (whose managed_repo import carries native unions) AND 3.11+ tomllib —
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
                "import sys; sys.path.insert(0, sys.argv[1]); import mode_posture_probe",
                str(_SCRIPTS_DIR),
            ],
            capture_output=True,
            text=True,
            env=_env_with_executable_on_path(),
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, (
            f"mode_posture_probe failed to import under the selector: {result.stderr.strip()}"
        )


class TestInterpreterPinIsLoadBearing:
    """The reported module crashes under < 3.11 — which is why the pin exists."""

    def test_reported_module_crashes_under_legacy_python(self) -> None:
        legacy = _legacy_python()
        if legacy is None:
            pytest.skip("no Python 3.9/3.10 interpreter available to demonstrate the crash")
        result = _import_under(legacy, "mode_posture_probe")
        assert result.returncode != 0, (
            f"expected mode_posture_probe to fail importing under {legacy} (PEP-604 union "
            f"evaluated at module load on < 3.11); it imported cleanly, so the pin would be vacuous"
        )

    def test_reported_module_imports_under_a_modern_interpreter(self) -> None:
        # The contrast to the test above: under this (>= 3.13) interpreter — the
        # kind the selector picks — the same module imports without error.
        result = _import_under(sys.executable, "mode_posture_probe")
        assert result.returncode == 0, (
            f"mode_posture_probe should import under {sys.executable}: {result.stderr.strip()}"
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


def _stub_interpreter(path: Path, *, label: str, has_django: bool, clears_floor: bool = True) -> None:
    """Write a stub interpreter that answers the selector's probes, then names itself.

    The selector probes with ``-c '...version_info...'`` and ``-c 'import django'``
    before ``exec``-ing the winner with the forwarded arguments. The stub keys off
    those probe bodies and, for any other ``-c`` payload, prints *label* — so a
    test reads which interpreter was chosen straight from stdout.
    """
    path.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        f"  *version_info*) exit {0 if clears_floor else 1} ;;\n"
        f'  *"import django"*) exit {0 if has_django else 1} ;;\n'
        "esac\n"
        f'printf "%s\\n" "{label}"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_selector(bin_dir: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    """Run the selector with *bin_dir* as the ENTIRE PATH, asking the winner to identify itself.

    ``bash`` is passed explicitly rather than left to the ``#!/usr/bin/env bash``
    shebang: PATH is deliberately narrowed to the stub dir so only the stub
    interpreters are discoverable, which would otherwise leave ``env`` unable to
    resolve the shell itself.
    """
    env = {"PATH": str(bin_dir), "HOME": str(bin_dir.parent)}
    env.update(extra_env)
    return subprocess.run(
        [_BASH, str(_RUN_HOOK), "-c", "identify-me"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


class TestRunHookPrefersDjangoCapableInterpreter:
    """The selector prefers an interpreter that can import Django, and degrades safely."""

    def test_prefers_the_teatree_venv_over_a_bare_python3(self, tmp_path: Path) -> None:
        # The live shape of the bug: a bare `python3` clears the version floor but
        # has no Django, while the venv teatree is installed into (found beside the
        # resolved `t3` entry point) has it. Choosing `python3` is what made every
        # DB-backed handler a silent no-op.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub_interpreter(bin_dir / "python3", label="bare-python3", has_django=False)
        _stub_interpreter(bin_dir / "python", label="teatree-venv", has_django=True)
        (bin_dir / "t3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / "t3").chmod(0o755)

        result = _run_selector(bin_dir)

        assert result.stdout.strip() == "teatree-venv", (
            f"selector must prefer the Django-capable interpreter; chose {result.stdout.strip()!r}"
        )

    def test_falls_back_to_the_version_floor_when_nothing_has_django(self, tmp_path: Path) -> None:
        # Fail open: on a host with no Django-capable interpreter the selector must
        # behave exactly as it did before — a hook that runs Django-free gates and
        # the file-mirror hand-off fallback beats a hook that does not run at all.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub_interpreter(bin_dir / "python3", label="bare-python3", has_django=False)

        result = _run_selector(bin_dir)

        assert result.stdout.strip() == "bare-python3", (
            f"selector must still pick a Django-less interpreter when it is all there is; "
            f"chose {result.stdout.strip()!r}, stderr={result.stderr!r}"
        )

    def test_explicit_override_outranks_the_django_preference(self, tmp_path: Path) -> None:
        # `T3_HOOK_PYTHON` is the operator's escape hatch — including when the
        # Django-capable pick is itself the thing misbehaving — so it wins on the
        # version floor alone.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub_interpreter(bin_dir / "python", label="teatree-venv", has_django=True)
        (bin_dir / "t3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / "t3").chmod(0o755)
        override = bin_dir / "chosen-by-operator"
        _stub_interpreter(override, label="operator-override", has_django=False)

        result = _run_selector(bin_dir, T3_HOOK_PYTHON=str(override))

        assert result.stdout.strip() == "operator-override", (
            f"T3_HOOK_PYTHON must outrank the search; chose {result.stdout.strip()!r}"
        )

    def test_unusable_override_falls_through_to_the_search(self, tmp_path: Path) -> None:
        # A stale override must not wedge every hooked session.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub_interpreter(bin_dir / "python3", label="bare-python3", has_django=True)

        result = _run_selector(bin_dir, T3_HOOK_PYTHON=str(tmp_path / "does-not-exist"))

        assert result.stdout.strip() == "bare-python3", (
            f"an unusable T3_HOOK_PYTHON must fall through, not wedge; got {result.stdout.strip()!r}"
        )

    def test_exits_silently_when_no_candidate_clears_the_version_floor(self, tmp_path: Path) -> None:
        # The crash-proof contract: a no-op hook, never a session-breaking crash.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub_interpreter(bin_dir / "python3", label="too-old", has_django=False, clears_floor=False)

        result = _run_selector(bin_dir)

        assert result.returncode == 0, f"selector must exit 0 when it finds nothing usable, got {result.returncode}"
        assert not result.stdout.strip(), f"selector must stay silent, printed {result.stdout!r}"
