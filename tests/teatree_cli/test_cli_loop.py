"""Tests for the ``t3 loop`` CLI commands (non-Django: start, stop, status, tick, cadence).

The automated per-loop tick (``t3 loops tick --loop <name>``) lives in
``teatree_core/test_loops_tick_command.py``. ``t3 loop tick`` here is the restored
user-manual full-scan diagnostic (autonomous-lane redesign §7) — it delegates to
the ``loop_tick`` management command, not the master-tick-free ``loops_tick``.
"""

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.agents import permission_modes
from teatree.cli.loop import _self_improve_cadence_for_loop_slot, loop_app
from teatree.cli.loop.drain_queue import _drain_cadence_for_loop_slot
from teatree.cli.loop.slack_answer import _slack_answer_cadence_for_loop_slot
from teatree.loops.fleet_policy import OWNER_INTAKE_LOOPS

runner = CliRunner()


def _seed_config_db(db_path: Path, **rows: object) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


class TestTickCommandDelegation:
    """``t3 loop tick`` — the restored user-manual full-scan tick (autonomous-lane redesign §7).

    Delegates to the ``loop_tick`` management command (NOT the per-loop
    ``loops_tick``), so it scans by hand without an owner lease or a ``--loop``.
    """

    def test_no_flags_delegates_to_loop_tick(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["tick"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_tick")

    def test_flags_forwarded_to_loop_tick(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(
                loop_app,
                ["tick", "--overlay", "teatree", "--json", "--statusline-file", "/tmp/sl.txt"],
            )
        assert result.exit_code == 0
        call_mock.assert_called_once_with(
            "loop_tick", statusline_file="/tmp/sl.txt", overlay="teatree", json_output=True
        )


class TestPendingSpawnCommandDelegation:
    # [skill-load-ok: pure CLI delegation test, no web framework involved]
    """``t3 loop pending-spawn`` forwards its flags to the management command.

    TODO #100: ``--claimable-only`` makes the Stop-hook self-pump's probe
    budget-aware so it stops re-offering an un-advanceable PENDING unit.
    """

    def test_no_flags_calls_with_empty_kwargs(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["pending-spawn"])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_dispatch", "pending-spawn")

    def test_json_flag_forwarded(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["pending-spawn", "--json"])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_dispatch", "pending-spawn", json_output=True)

    def test_claimable_only_flag_forwarded(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["pending-spawn", "--json", "--claimable-only"])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_dispatch", "pending-spawn", json_output=True, claimable_only=True)


class TestStatusCommand:
    def test_returns_one_when_no_statusline_file_yet(self, tmp_path: Path) -> None:
        with patch("teatree.cli.loop.app.default_path", return_value=tmp_path / "missing.txt"):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 1
        assert "No statusline rendered yet" in result.stdout

    def test_emits_file_contents_when_present(self, tmp_path: Path) -> None:
        statusline_file = tmp_path / "sl.txt"
        statusline_file.write_text("running 0.0.1\n→ check 1\n", encoding="utf-8")
        with patch("teatree.cli.loop.app.default_path", return_value=statusline_file):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 0
        assert "running 0.0.1" in result.stdout
        assert "check 1" in result.stdout

    def _statusline_with_meta(self, tmp_path: Path, *, rendered_at: float) -> Path:
        statusline_file = tmp_path / "sl.txt"
        statusline_file.write_text("t3-teatree 3m · next tick 4m\n", encoding="utf-8")
        (tmp_path / "tick-meta.json").write_text(
            f'{{"cadence": 720, "rendered_at": {rendered_at}}}\n', encoding="utf-8"
        )
        return statusline_file

    def test_prepends_stale_banner_for_frozen_render(self, tmp_path: Path) -> None:
        statusline_file = self._statusline_with_meta(tmp_path, rendered_at=time.time() - 6 * 3600)
        with patch("teatree.cli.loop.app.default_path", return_value=statusline_file):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 0
        assert "statusline STALE" in result.stdout
        # The frozen content still follows the banner.
        assert "next tick 4m" in result.stdout

    def test_no_banner_for_fresh_render(self, tmp_path: Path) -> None:
        statusline_file = self._statusline_with_meta(tmp_path, rendered_at=time.time() - 30)
        with patch("teatree.cli.loop.app.default_path", return_value=statusline_file):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 0
        assert "statusline STALE" not in result.stdout
        assert "next tick 4m" in result.stdout


class TestStartCommand:
    def test_print_only_emits_worker_owned_cadence_guidance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(loop_app, ["start", "--print-only"])

        assert result.exit_code == 0
        # PR-28: the worker owns the cadence; the guidance points at the worker status
        # + per-loop enable/disable, NOT the retired claude-spec cron-mirror path.
        assert "t3 worker status" in result.stdout
        assert "t3 loop enable|disable" in result.stdout
        assert "reactive infra loops" in result.stdout
        assert "--slot" not in result.stdout

    def test_inside_claude_session_falls_back_to_print(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        result = runner.invoke(loop_app, ["start"])

        assert result.exit_code == 0
        assert "t3 worker status" in result.stdout

    def test_missing_claude_binary_exits_with_guidance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        with (
            patch("teatree.cli.loop.app._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.app.shutil.which", return_value=None),
        ):
            result = runner.invoke(loop_app, ["start"])

        assert result.exit_code == 1
        assert "claude` not found" in result.stdout
        assert "t3 worker status" in result.stdout

    def test_spawns_claude_without_a_fat_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        with (
            patch("teatree.cli.loop.app._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.app.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.cli.loop.app.os.execv") as execv_mock,
        ):
            runner.invoke(loop_app, ["start"])

        assert execv_mock.call_count == 1
        argv = execv_mock.call_args.args[1]
        assert argv[0] == "/usr/bin/claude"
        # #2650: no single fat `/loop` slot is passed — the owner hook registers per-loop.
        assert not any(str(a).startswith("/loop") for a in argv)


class TestStartCommandSessionPins:
    """`t3 loop start` injects the session model/effort pins into the interactive spawn.

    These are the main-agent pins (so the user never runs `/model` manually):
    `--model <session_model>` and `--effort <session_effort>` go into the
    interactive `claude` os.execv argv, NOT into `claude -p` headless.
    """

    def _spawn_argv(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        session_model: str = "",
        session_effort: str = "",
    ) -> list[str]:
        rows: dict[str, object] = {}
        if session_model:
            rows["agent_session_model"] = session_model
        if session_effort:
            rows["agent_session_effort"] = session_effort
        db = tmp_path / "config.sqlite3"
        _seed_config_db(db, **rows)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setenv("T3_LOOP_CADENCE", "600")
        with (
            patch("teatree.cli.loop.app._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.app.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.cli.loop.app.os.execv") as execv_mock,
        ):
            runner.invoke(loop_app, ["start"])
        return list(execv_mock.call_args.args[1])

    def test_session_model_and_effort_injected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        argv = self._spawn_argv(
            monkeypatch,
            tmp_path,
            session_model="opus",
            session_effort="xhigh",
        )
        assert argv[0] == "/usr/bin/claude"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"
        assert "--effort" in argv
        assert argv[argv.index("--effort") + 1] == "xhigh"
        # #2650: no fat `/loop` slot — just the binary + pins.
        assert not any(str(a).startswith("/loop") for a in argv)

    def test_only_effort_injected_when_only_effort_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        argv = self._spawn_argv(monkeypatch, tmp_path, session_effort="max")
        assert "--effort" in argv
        assert argv[argv.index("--effort") + 1] == "max"
        assert "--model" not in argv

    def test_model_and_default_effort_injected_when_only_model_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # session_effort ships as xhigh, so a model-only config still injects the
        # default effort alongside the pinned model.
        argv = self._spawn_argv(monkeypatch, tmp_path, session_model="opus")
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--effort") + 1] == "xhigh"

    def test_default_effort_injected_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # No [agent] config → no model pin, but the shipped xhigh effort default
        # is always injected. The permission mode is pinned regardless.
        argv = self._spawn_argv(monkeypatch, tmp_path)
        assert "--model" not in argv
        assert argv[argv.index("--effort") + 1] == "xhigh"
        # #2650: no fat `/loop` slot — just the binary + pins.
        assert argv == ["/usr/bin/claude", "--permission-mode", "bypassPermissions", "--effort", "xhigh"]

    def test_session_model_passes_through_unchanged(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # #2237 removal: no kill-switch downgrade step — session_model is emitted
        # exactly as configured, whatever value the operator wrote.
        argv = self._spawn_argv(monkeypatch, tmp_path, session_model="claude-sonnet-5")
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5"


class TestStartCommandPinsUnattendedPermissionMode(TestStartCommandSessionPins):
    """The loop session pins its own permission mode instead of inheriting the operator's.

    `permissions.defaultMode` is a single global key in `~/.claude/settings.json`, and
    `t3 doctor check` advises setting it to `auto` for the session the operator drives.
    This session is NOT that session — it runs the autonomous loop, unattended under
    `autonomous_away`, where a classifier denial has nobody to override it. The pin is
    what makes the doctor's advice safe to follow, so it must survive every config
    combination rather than only the configured-`[agent]` path.
    """

    def test_mode_is_pinned_regardless_of_agent_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        for rows in ({}, {"session_model": "opus"}, {"session_effort": "max"}):
            argv = self._spawn_argv(monkeypatch, tmp_path, **rows)
            assert "--permission-mode" in argv, f"unattended session left unpinned for {rows}"
            assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"

    def test_mode_matches_the_headless_lane(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The two unattended lanes must not drift: both read the same constant.
        argv = self._spawn_argv(monkeypatch, tmp_path)
        assert argv[argv.index("--permission-mode") + 1] == permission_modes.UNATTENDED

    def test_auto_in_user_settings_cannot_reach_this_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The regression this guards: an operator follows the doctor's advice, sets
        # defaultMode=auto, and silently classifier-gates the autonomous loop.
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))
        argv = self._spawn_argv(monkeypatch, tmp_path)
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


class TestStopCommand:
    def test_stop_explains_unregister(self) -> None:
        result = runner.invoke(loop_app, ["stop"])

        assert result.exit_code == 0
        assert "/loop unregister t3-loop" in result.stdout


class TestClaimNextCommand:
    """#1107 Prong C — ``t3 loop claim-next`` must exist and delegate.

    The ``loop_dispatch`` mgmt command DOES expose a ``claim-next``
    subcommand (the #786 WS1 atomic claim), and the BLUEPRINT, the
    Stop-hook self-pump, ``cli/loop.py`` help, and ``slack_answer/cycle``
    all standardise on ``t3 loop claim-next`` — but ``cli/loop.py`` only
    wired ``pending-spawn``/``spawn-claim``, so the canonical command
    errored "No such command".
    """

    def test_loop_claim_next_command_exists_and_delegates(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call,
            patch("teatree.loop.session_identity.current_session_id", return_value="sess-cli"),
        ):
            result = runner.invoke(loop_app, ["claim-next", "--json"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_dispatch", "claim-next", claimed_by_session="sess-cli", json_output=True)

    def test_loop_claim_next_passes_claimed_by(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call,
            patch("teatree.loop.session_identity.current_session_id", return_value="sess-cli"),
        ):
            result = runner.invoke(loop_app, ["claim-next", "--claimed-by", "worker-7"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with(
            "loop_dispatch",
            "claim-next",
            claimed_by_session="sess-cli",
            claimed_by="worker-7",
        )

    def test_loop_claim_next_defaults_session_to_current_session_id(self) -> None:
        """#1917: an unset ``--claimed-by-session`` is resolved to the active session id."""
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call,
            patch("teatree.loop.session_identity.current_session_id", return_value="sess-auto"),
        ):
            result = runner.invoke(loop_app, ["claim-next"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_dispatch", "claim-next", claimed_by_session="sess-auto")

    def test_loop_claim_next_passes_explicit_session(self) -> None:
        """#1917: an explicit ``--claimed-by-session`` overrides the default resolution."""
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call,
            patch("teatree.loop.session_identity.current_session_id", return_value="should-not-be-used"),
        ):
            result = runner.invoke(loop_app, ["claim-next", "--claimed-by-session", "sess-explicit"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_dispatch", "claim-next", claimed_by_session="sess-explicit")

    def test_loop_claim_next_empty_session_when_unresolvable(self) -> None:
        """#1917 inert: when no session resolves, an empty session is threaded through."""
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call,
            patch("teatree.loop.session_identity.current_session_id", return_value=""),
        ):
            result = runner.invoke(loop_app, ["claim-next"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_dispatch", "claim-next", claimed_by_session="")


class TestSlackAnswerCadenceParser:
    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("20", "20s"),  # a deliberate low override is still honoured
            ("60", "1m"),
            ("", "5m"),  # unset → 300s fallback default
            ("garbage", "5m"),  # invalid → 300s fallback default
            ("5", "15s"),  # clamped to 15s floor
            ("15", "15s"),
        ],
    )
    def test_parses_t3_slack_answer_cadence(
        self, env_value: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", env_value)
        assert _slack_answer_cadence_for_loop_slot() == expected

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_SLACK_ANSWER_CADENCE", raising=False)
        assert _slack_answer_cadence_for_loop_slot() == "5m"


class TestSlackAnswerStartCommand:
    def test_start_emits_third_slot_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", "20")
        result = runner.invoke(loop_app, ["slack-answer", "start"])

        assert result.exit_code == 0
        assert "/loop 20s Run `t3 loop slack-answer run`." in result.stdout
        assert "T3_SLACK_ANSWER_CADENCE" in result.stdout

    def test_run_delegates_to_management_command(self) -> None:
        with patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["slack-answer", "run", "--json"])

        assert result.exit_code == 0
        call.assert_called_once_with("loop_slack_answer", json_output=True)


class TestDrainQueueCadenceParser:
    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("30", "30s"),
            ("60", "1m"),
            ("", "30s"),
            ("garbage", "30s"),
            ("5", "10s"),  # clamped to 10s floor
            ("10", "10s"),
        ],
    )
    def test_parses_t3_queue_drain_cadence(
        self, env_value: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_QUEUE_DRAIN_CADENCE", env_value)
        assert _drain_cadence_for_loop_slot() == expected

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_QUEUE_DRAIN_CADENCE", raising=False)
        assert _drain_cadence_for_loop_slot() == "30s"


class TestDrainQueueStartCommand:
    def test_start_emits_the_drain_slot_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_QUEUE_DRAIN_CADENCE", "30")
        result = runner.invoke(loop_app, ["drain-queue", "start"])

        assert result.exit_code == 0
        assert "/loop 30s Run `t3 loop drain-queue run`." in result.stdout
        assert "T3_QUEUE_DRAIN_CADENCE" in result.stdout

    def test_run_delegates_to_management_command(self) -> None:
        with patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["drain-queue", "run", "--json"])

        assert result.exit_code == 0
        call.assert_called_once_with("loop_drain_queue", json_output=True)


class TestSelfImproveCadenceParser:
    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("1800", "30m"),
            ("90", "90s"),
            ("60", "1m"),
            ("", "30m"),
            ("garbage", "30m"),
            ("5", "1m"),  # clamped to 60s floor
        ],
    )
    def test_parses_t3_self_improve_cadence(
        self, env_value: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_SELF_IMPROVE_CHEAP_CADENCE", env_value)
        assert _self_improve_cadence_for_loop_slot() == expected

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_SELF_IMPROVE_CHEAP_CADENCE", raising=False)
        assert _self_improve_cadence_for_loop_slot() == "30m"


class TestSelfImproveStartCommand:
    def test_start_emits_the_self_improve_slot_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SELF_IMPROVE_CHEAP_CADENCE", "1800")
        result = runner.invoke(loop_app, ["self-improve", "start"])

        assert result.exit_code == 0
        assert "/loop 30m Run `t3 loop self-improve run --tier cheap`." in result.stdout
        assert "T3_SELF_IMPROVE_CHEAP_CADENCE" in result.stdout


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestLoopOwnerCli:
    """``t3 loop claim/owner/release`` end-to-end through the mgmt command (#1073)."""

    def test_claim_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "cli-session")
        result = runner.invoke(loop_app, ["claim"])

        assert result.exit_code == 0, result.stdout
        assert "claimed loop slot" in result.stdout
        assert LoopLease.objects.get(name="t3-master").session_id == "cli-session"

    def test_claim_without_session_id_exits_2(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        for key in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "T3_LOOP_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "no-registry"))
        result = runner.invoke(loop_app, ["claim"])

        assert result.exit_code == 2
        assert "refusing to claim loop ownership without a Claude session id" in result.stdout

    def test_owner_reports_live_holder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="held-by")
        result = runner.invoke(loop_app, ["owner"])

        assert result.exit_code == 0
        assert "held-by" in result.stdout

    def test_owner_reports_unclaimed(self) -> None:
        result = runner.invoke(loop_app, ["owner"])

        assert result.exit_code == 0
        assert "unclaimed" in result.stdout

    def test_release_only_clears_own_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="other-session")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "me")
        result = runner.invoke(loop_app, ["release"])

        assert result.exit_code == 0
        assert "nothing released" in result.stdout
        assert LoopLease.objects.get(name="t3-master").session_id == "other-session"

    def test_release_clears_when_holder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "me")
        LoopLease.objects.claim_ownership("t3-master", session_id="me")
        result = runner.invoke(loop_app, ["release"])

        assert result.exit_code == 0
        assert "released loop slot" in result.stdout
        assert LoopLease.objects.get(name="t3-master").session_id == ""

    def test_take_over_seizes_live_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="hijacker")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "main")
        result = runner.invoke(loop_app, ["claim", "--take-over"])

        assert result.exit_code == 0
        assert "claimed loop slot" in result.stdout
        assert LoopLease.objects.get(name="t3-master").session_id == "main"

    def test_claim_without_take_over_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="hijacker")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "main")
        result = runner.invoke(loop_app, ["claim"])

        assert result.exit_code == 0
        assert "held by session hijacker" in result.stdout
        assert LoopLease.objects.get(name="t3-master").session_id == "hijacker"

    def test_owner_json_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("t3-master", session_id="json-sess")
        result = runner.invoke(loop_app, ["owner", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["owner_session"] == "json-sess"
        assert payload["is_live"] is True

    def test_custom_slot_is_independent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "answer-sess")
        result = runner.invoke(loop_app, ["claim", "--slot", "loop-slack-answer-owner"])

        assert result.exit_code == 0
        assert LoopLease.objects.get(name="loop-slack-answer-owner").session_id == "answer-sess"

    def test_claim_driver_external_sets_the_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exact shape of the DRIVERLESS remediation `t3 loop claim --slot <slot> --driver external`.
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred

        monkeypatch.setenv("CLAUDE_SESSION_ID", "ext-session")
        result = runner.invoke(loop_app, ["claim", "--slot", "loop:dispatch", "--driver", "external"])

        assert result.exit_code == 0, result.stdout
        assert LoopLease.objects.get(name="loop:dispatch").driver == "external"

    def test_claim_invalid_driver_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", "ext-session")
        result = runner.invoke(loop_app, ["claim", "--driver", "bogus"])

        assert result.exit_code == 2
        assert "invalid --driver" in result.stdout

    def test_claim_json_success_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "json-claimer")
        monkeypatch.setattr("teatree.loop.driver_detection.detect_driver", lambda _s: "")
        result = runner.invoke(loop_app, ["claim", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload == {
            "ok": True,
            "slot": "t3-master",
            "owner_session": "json-claimer",
            "driver": "",
            "driverless": True,
        }

    def test_claim_json_no_session_id_error_shape(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import json  # noqa: PLC0415

        for key in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "T3_LOOP_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "no-registry"))
        result = runner.invoke(loop_app, ["claim", "--json"])

        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "without a Claude session id" in payload["error"]

    def test_release_json_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "rel-sess")
        LoopLease.objects.claim_ownership("t3-master", session_id="rel-sess")
        result = runner.invoke(loop_app, ["release", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"ok": True, "slot": "t3-master"}


class TestIntakeLoopsCommand:
    """``t3 loop intake-loops`` prints the owner-intake names the fleet policy reads (#3632)."""

    def test_prints_owner_intake_names_sorted(self) -> None:
        result = runner.invoke(loop_app, ["intake-loops"])

        assert result.exit_code == 0
        assert result.stdout.split() == sorted(OWNER_INTAKE_LOOPS)
        assert "directive_loop" in result.stdout
