"""The Slack Socket-Mode receiver runs as its own Docker service.

Inbound Slack (a DM reply, a mention, an emoji reaction) only reaches the loop
when a Socket-Mode listener is running to feed the queue the worker drains.
`deploy/entrypoint.sh` gains a `slack-listener` role that execs `t3 slack
listen`, and `deploy/docker-compose.yml` gains the `teatree-slack-listener`
service that runs it. The receiver needs `slack_sdk`, so the init role's
editable install must pull the `[slack]` extra — without it `t3 slack listen`
degrades to a silent no-op ("slack_sdk not installed") and inbound Slack is
never seen.

Structure is parsed from the deploy sources directly (the source of truth),
mirroring `tests/test_deploy_bindmount_compose.py`.
"""

from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
ENTRYPOINT = (DEPLOY / "entrypoint.sh").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))

SHARED_DB_MOUNT = "/home/teatree/.local/share/teatree"


class TestInitInstallsSlackExtra:
    def test_editable_install_pulls_the_slack_extra(self) -> None:
        # Without the [slack] extra slack_sdk is absent and the receiver logs
        # "slack_sdk not installed" then no-ops — inbound Slack never arrives.
        assert '"$CLONE_DIR[slack]"' in ENTRYPOINT


class TestSlackListenerRole:
    def test_role_execs_slack_listen(self) -> None:
        assert "slack-listener)" in ENTRYPOINT
        arm = ENTRYPOINT.split("slack-listener)", 1)[1].split(";;", 1)[0]
        assert "exec t3 slack listen" in arm

    def test_role_is_documented_and_validated(self) -> None:
        # The required-role prompt and the unknown-role guard both name it, so a
        # misspelled TEATREE_ROLE fails loud instead of silently doing nothing.
        assert "init, worker, admin, slack-listener" in ENTRYPOINT
        assert "init|worker|admin|slack-listener" in ENTRYPOINT


class TestComposeSlackListenerService:
    @property
    def _service(self) -> dict:
        return COMPOSE["services"]["teatree-slack-listener"]

    def test_service_runs_the_listener_role(self) -> None:
        assert self._service["environment"]["TEATREE_ROLE"] == "slack-listener"

    def test_service_waits_for_init(self) -> None:
        # The editable install (with the [slack] extra) happens in init; the
        # listener must not start before that completes on the shared clone.
        assert self._service["depends_on"]["teatree-init"]["condition"] == "service_completed_successfully"

    def test_service_restarts_unless_stopped(self) -> None:
        assert self._service["restart"] == "unless-stopped"

    def test_service_shares_the_db_bind_mount(self) -> None:
        # Via the *teatree-common anchor: the listener must read the SAME
        # overlays registry (the bind-mounted sqlite DB) the worker writes, or
        # it resolves a different set of Slack-enabled overlays.
        sources = {
            entry["source"]
            for entry in self._service["volumes"]
            if isinstance(entry, dict) and entry.get("type") == "bind"
        }
        assert SHARED_DB_MOUNT in sources


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
