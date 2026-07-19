"""Every ``t3 teatree <group> <sub>`` the entrypoint runs must be a registered CLI leaf.

The deploy entrypoint (init role) drives provisioning through the ``t3`` CLI. A
subcommand it invokes that is NOT registered in the overlay command tree makes
``t3`` exit ``No such command`` — and because worker/admin/slack-listener
``depends_on`` a *successful* init, one unregistered leaf bricks the whole
stack. That is exactly how a merge that dropped ``config_setting seed`` from
:data:`teatree.cli.django_groups.DJANGO_GROUPS` while its handler and the
entrypoint call both survived took the box down (#3435 restore).

This gate closes the class. It parses ``deploy/entrypoint.sh`` for every
``t3 teatree <group> <sub>`` literal (real invocations AND the copies embedded
in operator-facing error strings) and asserts each resolves against the
*introspected* Typer + overlay command tree — the same tree the live CLI
dispatches on. It is RED on any entrypoint-cited subcommand the CLI does not
expose and GREEN once it is registered.

A second test pins the init-resilience contract: :func:`seed_setting` is
NON-FATAL. A single failed provisioning seed warns to stderr and lets init
continue (the runtime falls back to the code default) rather than aborting under
``set -e`` and taking the stack down. It runs the shell function verbatim from
the entrypoint under a stub ``t3``, mirroring
``tests/test_deploy_entrypoint_disable_loops.py``.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from teatree.cli import app, register_overlay_commands
from teatree.cli_reference import command_paths

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"

# ``t3 teatree <group> <sub>`` — group may carry underscores (``config_setting``);
# the subcommand token stops at the first non-name char (quote, ``$``, space), so
# an embedded ``'t3 teatree db migrate'`` in an echo string is captured cleanly.
_INVOCATION = re.compile(r"\bt3 teatree ([a-z][a-z0-9_]*) ([a-z][a-z0-9_-]*)")


def _entrypoint_invocations() -> list[tuple[int, str, str]]:
    """Every ``(lineno, group, sub)`` the entrypoint names under ``t3 teatree``."""
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(ENTRYPOINT.read_text(encoding="utf-8").splitlines(), start=1):
        for group, sub in _INVOCATION.findall(line):
            found.append((lineno, group, sub))
    return found


@pytest.fixture(scope="module")
def registered_paths() -> set[str]:
    """The live ``t3 …`` command paths with the ``teatree`` overlay assembled."""
    register_overlay_commands(allowlist={"t3-teatree"})
    return command_paths(app)


def _invocation_id(case: tuple[int, str, str]) -> str:
    lineno, group, sub = case
    return f"L{lineno}-{group}-{sub}"


class TestEntrypointCommandsAreRegistered:
    def test_entrypoint_names_at_least_one_teatree_command(self) -> None:
        # Guards the gate itself: if the parse silently found nothing (the
        # entrypoint moved or the pattern drifted), the parametrized test below
        # would vacuously pass. The anchor invocation must always be present.
        invocations = _entrypoint_invocations()
        assert ("config_setting", "seed") in {(g, s) for _, g, s in invocations}, (
            "expected `t3 teatree config_setting seed` in deploy/entrypoint.sh — "
            "the parser or the entrypoint changed shape"
        )

    @pytest.mark.parametrize("case", _entrypoint_invocations(), ids=_invocation_id)
    def test_invocation_resolves(self, case: tuple[int, str, str], registered_paths: set[str]) -> None:
        lineno, group, sub = case
        path = f"t3 teatree {group} {sub}"
        assert path in registered_paths, (
            f"deploy/entrypoint.sh:{lineno} invokes `{path}`, but it is not a registered "
            f"CLI subcommand (introspected from the live Typer + overlay tree). Register it "
            f"in teatree.cli.django_groups.DJANGO_GROUPS['{group}'] — an entrypoint-invoked "
            f"command missing from the tree exits `No such command` and bricks init."
        )


def _extract_shell_function(name: str) -> str:
    """Verbatim source of shell function *name*, ``name() {`` to its column-0 ``}``."""
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _write_t3_stub(bin_dir: Path, *, fail: bool) -> None:
    """A ``t3`` shim for ``config_setting seed``; exits 2 when ``fail`` (mirrors a bad seed)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "t3"
    exit_line = "  echo 'refusing: not a known config setting' >&2\n  exit 2\n" if fail else "  exit 0\n"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "teatree" ] && [ "${2:-}" = "config_setting" ] && [ "${3:-}" = "seed" ]; then\n'
        f"{exit_line}"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_seed_setting(tmp_path: Path, *, fail: bool) -> subprocess.CompletedProcess[str]:
    """Run the extracted ``seed_setting`` once under ``set -e`` with the stub on PATH."""
    bin_dir = tmp_path / "bin"
    _write_t3_stub(bin_dir, fail=fail)
    func = _extract_shell_function("seed_setting")
    harness = tmp_path / "harness.sh"
    harness.write_text(f"set -euo pipefail\n{func}\nseed_setting agent_harness '\"claude_sdk\"'\n", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")
class TestSeedSettingIsNonFatal:
    def test_happy_path_exits_zero_and_is_quiet(self, tmp_path: Path) -> None:
        result = _run_seed_setting(tmp_path, fail=False)
        assert result.returncode == 0, result.stderr
        assert "WARNING" not in result.stderr

    def test_failed_seed_warns_and_continues(self, tmp_path: Path) -> None:
        # The bricking scenario: the underlying seed exits non-zero. seed_setting
        # must swallow it — warn to stderr, return 0 — so `set -e` does NOT abort
        # init and take worker/admin/slack-listener down with it.
        result = _run_seed_setting(tmp_path, fail=True)
        assert result.returncode == 0, (
            "a single failed seed aborted init under `set -e` — one bad provisioning "
            f"value must not brick the stack. stderr:\n{result.stderr}"
        )
        assert "WARNING" in result.stderr
        assert "agent_harness" in result.stderr
