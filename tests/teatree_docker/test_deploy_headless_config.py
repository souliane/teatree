"""Guards on the committed deploy artifacts for #3232 / #3359.

Covers the container-wrapping ``deploy/t3`` entry (#3232 — executable, drives the
compose stack), the image-baked ``deploy/claude-settings.template.json`` (#3359 — valid
JSON carrying the minimum headless knobs: model, permission mode, autoMode grants,
autoCompact flag, tool-use concurrency), and the entrypoint/Dockerfile wiring that
provisions the settings file before ``t3 setup`` (statusLine-preserving deep
merge) and bakes the template.
"""

import json
import stat
from pathlib import Path

from teatree.cli.recommended_authorizations import RECOMMENDED_AUTHORIZATIONS
from teatree.docker.workflow import COMPOSE_REL, WRAPPER_REL

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"


class TestContainerWrappingEntry:
    def test_wrapper_matches_workflow_constant_and_exists(self) -> None:
        wrapper = REPO_ROOT / WRAPPER_REL
        assert wrapper.is_file()
        assert wrapper == DEPLOY / "t3"

    def test_wrapper_is_executable(self) -> None:
        mode = (REPO_ROOT / WRAPPER_REL).stat().st_mode
        assert mode & stat.S_IXUSR, "deploy/t3 must be executable to serve as the alias target"

    def test_wrapper_drives_the_compose_stack(self) -> None:
        text = (REPO_ROOT / WRAPPER_REL).read_text(encoding="utf-8")
        assert "docker compose" in text
        assert "teatree-worker" in text  # the default service it execs into

    def test_compose_stack_constant_resolves(self) -> None:
        assert (REPO_ROOT / COMPOSE_REL).is_file()


class TestHeadlessClaudeSettings:
    def _settings(self) -> dict:
        return json.loads((DEPLOY / "claude-settings.template.json").read_text(encoding="utf-8"))

    def test_is_valid_json(self) -> None:
        assert isinstance(self._settings(), dict)

    def test_carries_the_minimum_headless_knobs(self) -> None:
        data = self._settings()
        assert isinstance(data.get("model"), str)
        assert data["model"]
        assert data["permissions"]["defaultMode"]
        assert isinstance(data["permissions"]["allow"], list)
        assert isinstance(data["autoMode"]["allow"], list)
        assert data["autoMode"]["allow"]
        assert isinstance(data["autoCompactEnabled"], bool)
        # Concurrency is an env value — Claude Code reads env as strings.
        assert isinstance(data["env"]["CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"], str)

    def test_env_concurrency_is_a_positive_int(self) -> None:
        data = self._settings()
        assert int(data["env"]["CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"]) > 0

    def test_automode_carries_every_recommended_authorization(self) -> None:
        # #3408/#3410: the template's autoMode.allow is the single source both host
        # (`t3 setup --write-automode`) and container seed apply, so it must carry the
        # full recommended set — a fresh box is then classifier-unblocked everywhere.
        allow = self._settings()["autoMode"]["allow"]
        for rec in RECOMMENDED_AUTHORIZATIONS:
            assert rec.sentence in allow, f"template autoMode.allow is missing the {rec.key!r} recommendation"


class TestEntrypointAndDockerfileWiring:
    def test_entrypoint_seeds_settings_before_t3_setup(self) -> None:
        text = (DEPLOY / "entrypoint.sh").read_text(encoding="utf-8")
        assert "seed_claude_settings" in text
        # The seed call must immediately precede `t3 setup` so setup's statusLine
        # merge lands on top of the seeded file rather than being clobbered by it.
        assert "seed_claude_settings\n    t3 setup" in text

    def test_entrypoint_merge_preserves_unmanaged_keys(self) -> None:
        # Deep-merge with the existing file as the LEFT operand keeps statusLine.
        text = (DEPLOY / "entrypoint.sh").read_text(encoding="utf-8")
        assert "jq -s '.[0] * .[1]'" in text

    def test_dockerfile_bakes_the_template(self) -> None:
        text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        assert "deploy/claude-settings.template.json" in text

    def test_compose_comment_no_longer_conflates_settings_with_credentials(self) -> None:
        text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        # The stale claim that "settings stay host-only" must be gone.
        assert "credentials + settings stay host-only" not in text
        assert "#3359" in text
