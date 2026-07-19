# test-path: cross-cutting
"""Drain-then-deploy guardrails: the deploy plane never kills an in-flight agent.

Pins the two halves of the rolling deploy across the deploy artifacts so a future
edit cannot silently drop them.

Piece A (debounce): ``deploy.yml`` serializes on a fixed ``deploy`` group and NEVER
cancels a running convergence (``cancel-in-progress: false``); ``deploy.sh``
fast-forwards the checkout to latest main.

Piece B (drain): ``deploy.sh`` drains the running worker before the image swap;
``entrypoint.sh`` clears ``worker_quiescing`` on the fresh worker so admission
resumes; the worker gets a stop grace window for a clean shutdown.
"""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DEPLOY_YML = _ROOT / ".github" / "workflows" / "deploy.yml"
_DEPLOY_SH = _ROOT / "deploy" / "deploy.sh"
_ENTRYPOINT_SH = _ROOT / "deploy" / "entrypoint.sh"
_COMPOSE_YML = _ROOT / "deploy" / "docker-compose.yml"


def _deploy_workflow() -> dict:
    return yaml.safe_load(_DEPLOY_YML.read_text(encoding="utf-8"))


class TestDeployDebounce:
    def test_concurrency_group_is_the_fixed_deploy_group(self) -> None:
        assert str(_deploy_workflow()["concurrency"]["group"]) == "deploy", (
            "deploy.yml must serialize on ONE fixed 'deploy' group so a merge train "
            "coalesces onto the single box instead of racing convergences."
        )

    def test_never_cancels_a_running_convergence(self) -> None:
        cancel = _deploy_workflow()["concurrency"]["cancel-in-progress"]
        assert cancel is False, (
            "cancel-in-progress must be false — a superseding merge must never cancel a "
            "RUNNING convergence (an in-flight worker drain) mid-run."
        )

    def test_deploy_script_fast_forwards_to_latest_main(self) -> None:
        body = _DEPLOY_SH.read_text(encoding="utf-8")
        assert "fetch --prune origin" in body
        assert "pull --ff-only" in body


class TestDeployDrain:
    def test_deploy_script_drains_the_running_worker_before_the_swap(self) -> None:
        body = _DEPLOY_SH.read_text(encoding="utf-8")
        drain_at = body.find("t3 worker drain")
        swap_at = body.find("up -d --build")
        assert drain_at != -1, "deploy.sh must drain the worker before swapping the image"
        assert swap_at != -1
        assert drain_at < swap_at, "the drain must run BEFORE `docker compose up -d --build`"
        # Guarded by worker_running (nothing to drain otherwise) and non-fatal on overrun.
        assert "if worker_running; then" in body
        assert "TEATREE_DRAIN_TIMEOUT" in body

    def test_fresh_worker_init_clears_the_quiescing_gate(self) -> None:
        body = _ENTRYPOINT_SH.read_text(encoding="utf-8")
        assert "config_setting set worker_quiescing false" in body, (
            "entrypoint init must CLEAR worker_quiescing (a hard `set false`, not a "
            "provenance `seed`) so the fresh worker resumes admission after a deploy."
        )

    def test_worker_has_a_stop_grace_period(self) -> None:
        compose = yaml.safe_load(_COMPOSE_YML.read_text(encoding="utf-8"))
        assert "stop_grace_period" in compose["services"]["teatree-worker"], (
            "teatree-worker needs a stop_grace_period so a recreate lets the SIGTERM "
            "handler exit cleanly instead of SIGKILL at the 10s default."
        )
