# test-path: cross-cutting — drives deploy/entrypoint.sh + docker-compose.yml + the doctor drain-heartbeat contract.
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

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
ENTRYPOINT = (DEPLOY / "entrypoint.sh").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))

SHARED_DB_MOUNT = "/home/teatree/.local/share/teatree"


class TestInitInstallsSlackExtra:
    def test_editable_install_pulls_the_slack_extra(self) -> None:
        # Without the [slack] extra slack_sdk is absent and the receiver logs
        # "slack_sdk not installed" then no-ops — inbound Slack never arrives.
        # Braced form (`${CLONE_DIR}`) so the expansion is shellcheck-clean (SC1087).
        assert '--editable "${CLONE_DIR}[slack]"' in ENTRYPOINT


_BASH = shutil.which("bash") or "bash"
_TIMEOUT = shutil.which("timeout") or "timeout"


def _drain_loop_body() -> str:
    """The verbatim source of the top-level ``slack_drain_loop`` shell function."""
    lines: list[str] = []
    capturing = False
    for line in ENTRYPOINT.splitlines():
        if line.startswith("slack_drain_loop() {"):
            capturing = True
        if capturing:
            lines.append(line)
            if line == "}":
                return "\n".join(lines)
    not_found = "slack_drain_loop function not found in entrypoint.sh"
    raise AssertionError(not_found)


class TestSlackListenerRole:
    @property
    def _arm(self) -> str:
        return ENTRYPOINT.split("slack-listener)", 1)[1].split(";;", 1)[0]

    def test_role_execs_slack_listen(self) -> None:
        assert "slack-listener)" in ENTRYPOINT
        assert "exec t3 slack listen" in self._arm

    def test_role_drains_captured_dms_on_a_cadence(self) -> None:
        # The reactive loop-drain-queue slot is not bootstrapped under `t3
        # worker` in headless, so without a periodic `t3 slack check` the
        # listener's captured DMs never reach an observable (👀-acked) state.
        # `t3 slack check` drains the JSONL queue and is NOT worker-singleton
        # gated (unlike the drain-queue loop).
        body = _drain_loop_body()
        assert "t3 slack check" in body
        assert "while true; do" in body, "the drain must run on a repeating cadence, not once"

    def test_drain_loop_is_backgrounded_before_the_foreground_exec(self) -> None:
        # `slack_drain_loop &` backgrounds the cadence so `exec t3 slack listen`
        # stays the foreground process; it must start BEFORE the exec, or exec
        # would replace the shell before the loop is ever launched.
        arm = self._arm
        assert "slack_drain_loop &" in arm
        assert "&\n" in arm, "the drain loop must be backgrounded"
        # `rindex` for the exec: an earlier mention lives in the explanatory comment.
        assert arm.index("slack_drain_loop &") < arm.rindex("exec t3 slack listen")

    def test_drain_failures_are_surfaced_not_swallowed(self) -> None:
        # #3443: the old `>/dev/null 2>&1 || true` hid every error. The loop must
        # now log real failures to stderr with a consecutive-failure counter and
        # never re-introduce the output-swallowing form in the active loop body.
        body = _drain_loop_body()
        assert "t3 slack check >/dev/null 2>&1 || true" not in body
        assert ">&2" in body, "drain failures must be logged to stderr"
        assert "consecutive" in body, "the loop must track a consecutive-failure counter"

    def test_drain_writes_a_heartbeat_doctor_reads(self) -> None:
        # The heartbeat filename is the doctor↔entrypoint contract; the doctor
        # side (`self_heal_slack_drain._HEARTBEAT_FILENAME`) must name the same file.
        from teatree.cli.doctor.self_heal_slack_drain import _HEARTBEAT_FILENAME  # noqa: PLC0415 — test-local import

        body = _drain_loop_body()
        assert _HEARTBEAT_FILENAME in body
        assert "consecutive_failures" in body, "the heartbeat must carry the failure count doctor gates on"

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


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None,
    reason="needs bash + timeout (present in the deploy image and CI)",
)
class TestSlackDrainLoopExecution:
    """Run the REAL `slack_drain_loop` (extracted verbatim) with a stub `t3`.

    `t3 slack check` exits 1-with-no-output on an empty queue (healthy) and
    non-zero-with-output on a real failure; the loop must tell them apart, log
    only real failures to stderr, and record the streak in the heartbeat doctor
    reads.
    """

    def _run(self, tmp_path: Path, check_body: str) -> tuple[str, dict]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        t3 = bin_dir / "t3"
        # The stub answers only `t3 slack check`; anything else is a no-op success.
        t3.write_text(
            '#!/usr/bin/env bash\nif [ "$1 $2" = "slack check" ]; then\n' + check_body + "\nfi\nexit 0\n",
            encoding="utf-8",
        )
        t3.chmod(0o755)
        heartbeat = tmp_path / "hb.json"
        harness = tmp_path / "harness.sh"
        harness.write_text(f"set -euo pipefail\n{_drain_loop_body()}\nslack_drain_loop\n", encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["SLACK_CHECK_INTERVAL_SECONDS"] = "0.2"
        env["SLACK_DRAIN_HEARTBEAT"] = str(heartbeat)
        proc = subprocess.run(
            [_TIMEOUT, "1", _BASH, str(harness)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        beat = json.loads(heartbeat.read_text(encoding="utf-8")) if heartbeat.exists() else {}
        return proc.stderr, beat

    def test_real_failure_is_logged_and_counted(self, tmp_path: Path) -> None:
        stderr, beat = self._run(tmp_path, 'echo "Traceback: DB unreachable" >&2\nexit 2')
        assert "FAILED" in stderr
        assert "Traceback: DB unreachable" in stderr, "the real error output must reach the logs"
        assert beat.get("consecutive_failures", 0) >= 1

    def test_empty_queue_is_not_a_failure(self, tmp_path: Path) -> None:
        # exit 1 with NO output = empty queue on a quiet box; must not count/log.
        stderr, beat = self._run(tmp_path, "exit 1")
        assert "FAILED" not in stderr
        assert beat.get("consecutive_failures", -1) == 0

    def test_benign_stderr_warning_on_empty_queue_is_not_a_failure(self, tmp_path: Path) -> None:
        # Every t3 invocation emits a benign WARNING to STDERR (e.g. an overlay's
        # skills-root notice). On an empty queue (rc=1, empty STDOUT) that warning
        # must NOT be mistaken for a failure — the emptiness test keys on stdout,
        # not on stderr folded in via 2>&1.
        stderr, beat = self._run(
            tmp_path,
            'echo "WARNING teatree.cli.overlay skills root declared but no tool-commands.json found" >&2\nexit 1',
        )
        assert "FAILED" not in stderr
        assert beat.get("consecutive_failures", -1) == 0

    def test_rc1_with_stdout_content_is_a_failure(self, tmp_path: Path) -> None:
        # rc=1 but WITH stdout content is a real failure (a crash that printed to
        # stdout then exited 1), not an empty-queue poll.
        stderr, beat = self._run(tmp_path, 'echo "boot error on stdout"\nexit 1')
        assert "FAILED" in stderr
        assert "boot error on stdout" in stderr
        assert beat.get("consecutive_failures", 0) >= 1

    def test_drained_messages_reset_the_streak(self, tmp_path: Path) -> None:
        stderr, beat = self._run(tmp_path, 'echo "{\\"overlay\\": \\"acme\\"}"\nexit 0')
        assert "FAILED" not in stderr
        assert beat.get("consecutive_failures", -1) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
