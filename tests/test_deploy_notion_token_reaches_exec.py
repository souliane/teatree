"""The Notion token must reach a `docker exec`, and must never reach `teatree.env`.

Two halves, and they pull in opposite directions, which is why both are pinned here.

`deploy/entrypoint.sh` sources the token from ``pass`` and ``export``s it, but an
export reaches only the process tree of the role it ran for. ``docker exec`` starts
from the CONTAINER's create-time environment, so an exec'd `t3 notion whoami` sees an
unset ``NOTION_TOKEN`` while the worker had it the whole time — the same boundary
`test_deploy_gitlab_token_reaches_exec.py` pins for GitLab. The compose files DECLARE
it per service, and ``deploy/deploy.sh`` resolves it from the SAME default ``pass``
key the entrypoint uses, so one credential is named in one place.

The other half is what must NOT happen: ``deploy/teatree.env`` is regenerated wholesale
by the deploy workflow on every run and is deliberately secret-free, so a
``NOTION_TOKEN=`` line there is silently reverted on the next deploy and regresses the
no-plaintext-at-rest invariant besides.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"
DEPLOY_SH = DEPLOY / "deploy.sh"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
DEPLOY_WORKFLOW = REPO / ".github" / "workflows" / "deploy.yml"

DEFAULT_PASS_KEY = "notion/integration-token"
PASS_KEY_OVERRIDE = "NOTION_TOKEN_PASS_PATH"

# Every service an operator or the CLI wrapper `docker exec`s into. The watchdog is
# excluded here and asserted separately: it never runs teatree, it CARRIES the value
# so its own `compose up` repair can interpolate it.
EXEC_TARGET_SERVICES = ["teatree-init", "teatree-worker", "teatree-admin", "teatree-slack-listener"]


@pytest.fixture(scope="module")
def compose_doc() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _service_env(compose_doc: dict, service: str) -> dict[str, str]:
    return compose_doc["services"][service].get("environment") or {}


class TestEveryExecTargetDeclaresTheToken:
    """The declaration is the whole fix: an export cannot cross into a `docker exec`."""

    @pytest.mark.parametrize("service", EXEC_TARGET_SERVICES)
    def test_service_interpolates_and_defaults_empty(self, compose_doc: dict, service: str) -> None:
        # `:-` and not `?err`: a box with no Notion token must still bring the stack up.
        assert _service_env(compose_doc, service)["NOTION_TOKEN"] == "${NOTION_TOKEN:-}"

    def test_the_watchdog_carries_it_for_its_own_repair(self, compose_doc: dict) -> None:
        # Its repair is an inner `compose up`, which interpolates from ITS environment;
        # the host pass store is not mounted there, so it cannot re-derive the value.
        assert _service_env(compose_doc, "teatree-watchdog")["NOTION_TOKEN"] == "${NOTION_TOKEN:-}"

    def test_no_literal_token_is_committed(self, compose_doc: dict) -> None:
        declared = {
            service: env["NOTION_TOKEN"]
            for service, spec in compose_doc["services"].items()
            if "NOTION_TOKEN" in (env := spec.get("environment") or {})
        }
        assert declared, "no service declares NOTION_TOKEN — the exec boundary is open again"
        assert all(value.startswith("${") for value in declared.values())


class TestOneCredentialNamedInOnePlace:
    @pytest.mark.parametrize("script", [ENTRYPOINT, DEPLOY_SH])
    def test_script_reads_the_shared_default_key(self, script: Path) -> None:
        assert f"${{{PASS_KEY_OVERRIDE}:-{DEFAULT_PASS_KEY}}}" in script.read_text(encoding="utf-8")


class TestTheTokenNeverLandsInTheEnvFile:
    def test_the_only_writer_of_teatree_env_emits_no_notion_line(self) -> None:
        # The deploy workflow is the sole writer of that file and overwrites it
        # wholesale, so a line added there survives exactly until the next deploy —
        # and is plaintext at rest in the meantime.
        assert "NOTION_TOKEN" not in DEPLOY_WORKFLOW.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
