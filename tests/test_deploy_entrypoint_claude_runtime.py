# test-path: cross-cutting — drives deploy/entrypoint.sh (no src mirror).
"""Every agent-spawning role provisions the per-container Claude runtime.

The agent's skills ship as the ``t3@souliane`` Claude Code plugin, registered by
``t3 setup`` (PluginRegistrar) into ``~/.claude`` — which is PER-CONTAINER ephemeral
(docker-compose.yml bind-mounts only ``~/.claude/projects``). So init's registration
never reaches the worker/admin/slack-listener containers, and their agents load ZERO
skills unless each re-runs the claude-env prep itself. This asserts the entrypoint's
``prepare_claude_runtime`` (seed settings + ``t3 setup``) runs in EVERY role that
spawns ``claude`` — init, worker, admin, slack-listener — not only init.

A structural (text) test over ``deploy/entrypoint.sh`` — the role dispatch is a
shell ``case``, so the contract is which branches invoke the prep.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"
_PATH = os.environ.get("PATH", "")


def _role_branch_body(source: str, role: str) -> str:
    """Return the shell between ``<role>)`` and its terminating ``;;`` in the ROLE case."""
    case_start = source.index('case "$ROLE" in')
    body = source[case_start:]
    match = re.search(rf"(?m)^{re.escape(role)}\)\n(.*?)^\s*;;", body, re.DOTALL)
    assert match is not None, f"role branch {role!r} not found in the ROLE case"
    return match.group(1)


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
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


class TestPrepareClaudeRuntime:
    def test_prepare_function_seeds_settings_and_runs_setup(self) -> None:
        source = ENTRYPOINT.read_text(encoding="utf-8")
        match = re.search(r"(?m)^prepare_claude_runtime\(\) \{\n(.*?)^\}", source, re.DOTALL)
        assert match is not None, "prepare_claude_runtime() not defined"
        body = match.group(1)
        assert "seed_claude_settings" in body
        assert "t3 setup" in body

    def test_every_agent_spawning_role_prepares_the_claude_runtime(self) -> None:
        source = ENTRYPOINT.read_text(encoding="utf-8")
        for role in ("init", "worker", "admin", "slack-listener"):
            assert "prepare_claude_runtime" in _role_branch_body(source, role), (
                f"role {role!r} must run prepare_claude_runtime so its agent's ~/.claude has the t3 plugin/skills"
            )

    def test_runtime_roles_prepare_before_their_exec(self) -> None:
        # The prep must land BEFORE the role hands off to its long-running process.
        source = ENTRYPOINT.read_text(encoding="utf-8")
        for role, exec_marker in (
            ("worker", "exec t3 worker"),
            ("admin", "exec t3 admin"),
            ("slack-listener", "exec t3 slack listen"),
        ):
            body = _role_branch_body(source, role)
            # rindex: the exec command is the LAST occurrence (a comment may name it too).
            assert body.index("prepare_claude_runtime") < body.rindex(exec_marker)


class TestWorkerSkillsHardFail:
    """The worker REFUSES to start skill-less (owner: prefer hard fail over degraded run)."""

    def test_worker_branch_verifies_skills_and_exits_on_failure(self) -> None:
        body = _role_branch_body(ENTRYPOINT.read_text(encoding="utf-8"), "worker")
        assert "verify_agent_skills" in body
        # A missing-skills path must hard-exit non-zero, not warn-and-continue.
        assert re.search(r"verify_agent_skills.*?exit 1", body, re.DOTALL), (
            "worker must `exit 1` when verify_agent_skills fails"
        )
        assert "refusing to start" in body

    def test_verify_only_hard_fails_the_worker_role(self) -> None:
        # admin/slack-listener keep the non-fatal `|| ...` prep; only worker hard-exits.
        source = ENTRYPOINT.read_text(encoding="utf-8")
        for role in ("admin", "slack-listener"):
            assert "verify_agent_skills" not in _role_branch_body(source, role)


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="needs bash + jq (present in the deploy image and CI)",
)
class TestVerifyAgentSkillsFunction:
    """Run the REAL ``verify_agent_skills`` shell function against a stubbed ~/.claude."""

    def _run(self, tmp_path: Path, *, enabled: bool, installed: bool, install_dir_exists: bool = True) -> int:
        home = tmp_path / "home"
        claude = home / ".claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"t3@souliane": enabled}}), encoding="utf-8"
        )
        install_path = home / "clone"
        if install_dir_exists:
            install_path.mkdir()
        plugins = {"plugins": {"t3@souliane": [{"installPath": str(install_path)}]}} if installed else {"plugins": {}}
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(plugins), encoding="utf-8")

        func = _extract_shell_function("verify_agent_skills")
        harness = tmp_path / "harness.sh"
        harness.write_text(f"set -euo pipefail\n{func}\nverify_agent_skills\n", encoding="utf-8")
        proc = subprocess.run(
            [_BASH, str(harness)], capture_output=True, text=True, check=False, env={"HOME": str(home), "PATH": _PATH}
        )
        return proc.returncode

    def test_passes_when_enabled_and_installed(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, enabled=True, installed=True) == 0

    def test_fails_when_not_enabled(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, enabled=False, installed=True) != 0

    def test_fails_when_not_installed(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, enabled=True, installed=False) != 0

    def test_fails_when_install_path_missing(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, enabled=True, installed=True, install_dir_exists=False) != 0
