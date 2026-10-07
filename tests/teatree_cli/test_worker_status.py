"""``t3 worker status`` keeps answering when a side read fails, because deploy.sh greps its JSON for ``running``."""

import json
from unittest import mock

import django.test
from django.db import OperationalError
from typer.testing import CliRunner

import teatree.cli.worker as worker_cli
from teatree.cli.worker import worker_app
from teatree.loops.loop_staleness import Admission, LoopHealth

runner = CliRunner()


def _healthy_loop_health() -> LoopHealth:
    return LoopHealth(
        admission=Admission(mode="standard", source="default", admitted=("tickets",), admitted_total=1),
        stale=(),
        considered=1,
    )


class TestAnUnreadableDeployDrain(django.test.TestCase):
    def _status(self, *args: str) -> str:
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=4242),
            mock.patch("teatree.loops.loop_staleness.loop_health", return_value=_healthy_loop_health()),
            mock.patch("teatree.cli.worker_status.quiesce_status", side_effect=OperationalError("database is locked")),
        ):
            result = runner.invoke(worker_app, ["status", *args])
        assert result.exit_code == 0, result.output
        return result.stdout

    def test_the_json_still_reports_the_worker_running(self) -> None:
        payload = json.loads(self._status("--json"))

        assert payload["running"] is True
        assert payload["quiescing"] is None

    def test_the_text_says_the_drain_is_unavailable(self) -> None:
        assert "deploy drain: unavailable (OperationalError)" in self._status()
