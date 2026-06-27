"""Tests for the ``t3 loop`` CLI commands (non-Django: start, stop, status, cadence).

Tick-specific tests live in ``teatree_core/test_loop_tick_command.py`` since
tick is now a Django management command.
"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.loop import loop_app
from teatree.cli.loop_slack_answer import _slack_answer_cadence_for_loop_slot

runner = CliRunner()


class TestTickCommandDelegation:
    def test_delegates_to_management_command(self, tmp_path: Path) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["tick", "--statusline-file", str(tmp_path / "sl.txt")])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_tick", statusline_file=str(tmp_path / "sl.txt"))

    def test_passes_overlay_and_json_flags(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["tick", "--overlay", "myoverlay", "--json"])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_tick", overlay="myoverlay", json_output=True)

    def test_no_args_calls_with_empty_kwargs(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["tick"])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_tick")


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
        with patch("teatree.cli.loop.default_path", return_value=tmp_path / "missing.txt"):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 1
        assert "No statusline rendered yet" in result.stdout

    def test_emits_file_contents_when_present(self, tmp_path: Path) -> None:
        statusline_file = tmp_path / "sl.txt"
        statusline_file.write_text("running 0.0.1\n→ check 1\n", encoding="utf-8")
        with patch("teatree.cli.loop.default_path", return_value=statusline_file):
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
        with patch("teatree.cli.loop.default_path", return_value=statusline_file):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 0
        assert "statusline STALE" in result.stdout
        # The frozen content still follows the banner.
        assert "next tick 4m" in result.stdout

    def test_no_banner_for_fresh_render(self, tmp_path: Path) -> None:
        statusline_file = self._statusline_with_meta(tmp_path, rendered_at=time.time() - 30)
        with patch("teatree.cli.loop.default_path", return_value=statusline_file):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 0
        assert "statusline STALE" not in result.stdout
        assert "next tick 4m" in result.stdout


class TestStartCommand:
    def test_print_only_emits_per_loop_registration_guidance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = runner.invoke(loop_app, ["start", "--print-only"])

        assert result.exit_code == 0
        # #2650: per-loop registration via the claude-spec path + the /t3:loops skill,
        # NOT a single fat `/loop` slot.
        assert "t3 loop claude-spec" in result.stdout
        assert "t3 loops tick --loop" in result.stdout
        assert "/t3:loops" in result.stdout
        assert "--slot" not in result.stdout

    def test_inside_claude_session_falls_back_to_print(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        result = runner.invoke(loop_app, ["start"])

        assert result.exit_code == 0
        assert "t3 loop claude-spec" in result.stdout

    def test_missing_claude_binary_exits_with_guidance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        with (
            patch("teatree.cli.loop._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.shutil.which", return_value=None),
        ):
            result = runner.invoke(loop_app, ["start"])

        assert result.exit_code == 1
        assert "claude` not found" in result.stdout
        assert "t3 loop claude-spec" in result.stdout

    def test_spawns_claude_without_a_fat_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        with (
            patch("teatree.cli.loop._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.cli.loop.os.execv") as execv_mock,
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

    def _spawn_argv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_body: str) -> list[str]:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text(config_body, encoding="utf-8")
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setenv("T3_LOOP_CADENCE", "600")
        monkeypatch.setattr("teatree.config_agent.CONFIG_PATH", cfg)
        with (
            patch("teatree.cli.loop._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.cli.loop.os.execv") as execv_mock,
        ):
            runner.invoke(loop_app, ["start"])
        return list(execv_mock.call_args.args[1])

    def test_session_model_and_effort_injected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        argv = self._spawn_argv(
            monkeypatch,
            tmp_path,
            '[agent]\nsession_model = "fable"\nsession_effort = "xhigh"\n',
        )
        assert argv[0] == "/usr/bin/claude"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "fable"
        assert "--effort" in argv
        assert argv[argv.index("--effort") + 1] == "xhigh"
        # #2650: no fat `/loop` slot — just the binary + pins.
        assert not any(str(a).startswith("/loop") for a in argv)

    def test_only_effort_injected_when_only_effort_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        argv = self._spawn_argv(monkeypatch, tmp_path, '[agent]\nsession_effort = "max"\n')
        assert "--effort" in argv
        assert argv[argv.index("--effort") + 1] == "max"
        assert "--model" not in argv

    def test_only_model_injected_when_only_model_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        argv = self._spawn_argv(monkeypatch, tmp_path, '[agent]\nsession_model = "fable"\n')
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "fable"
        assert "--effort" not in argv

    def test_no_pins_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        argv = self._spawn_argv(monkeypatch, tmp_path, '[teatree]\nmode = "interactive"\n')
        assert "--model" not in argv
        assert "--effort" not in argv
        # #2650: no pins, no fat `/loop` slot — just the binary.
        assert argv == ["/usr/bin/claude"]

    def test_fable_session_model_downgrades_to_opus_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # teatree#2237: the kill-switch downgrades the session --model pin too.
        argv = self._spawn_argv(
            monkeypatch,
            tmp_path,
            '[agent]\nfable_enabled = false\nsession_model = "fable"\n',
        )
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"

    def test_fable_full_id_session_model_downgrades_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._spawn_argv(
            monkeypatch,
            tmp_path,
            '[agent]\nfable_enabled = false\nsession_model = "claude-fable-5"\n',
        )
        assert argv[argv.index("--model") + 1] == "opus"

    def test_fable_session_model_downgrades_to_fallback_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = self._spawn_argv(
            monkeypatch,
            tmp_path,
            '[agent]\nfable_enabled = false\nfable_fallback = "sonnet"\nsession_model = "fable"\n',
        )
        assert argv[argv.index("--model") + 1] == "sonnet"

    def test_fable_session_model_kept_when_enabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Toggle ON (and absent): the Fable session pin is byte-identical to today.
        argv = self._spawn_argv(monkeypatch, tmp_path, '[agent]\nsession_model = "fable"\n')
        assert argv[argv.index("--model") + 1] == "fable"


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
            ("20", "20s"),
            ("60", "1m"),
            ("", "20s"),
            ("garbage", "20s"),
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
        assert _slack_answer_cadence_for_loop_slot() == "20s"


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
        assert LoopLease.objects.get(name="loop-owner").session_id == "cli-session"

    def test_claim_without_session_id_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("T3_LOOP_SESSION_ID", raising=False)
        result = runner.invoke(loop_app, ["claim"])

        assert result.exit_code == 2
        assert "refusing to claim loop ownership without a Claude session id" in result.stdout

    def test_owner_reports_live_holder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="held-by")
        result = runner.invoke(loop_app, ["owner"])

        assert result.exit_code == 0
        assert "held-by" in result.stdout

    def test_owner_reports_unclaimed(self) -> None:
        result = runner.invoke(loop_app, ["owner"])

        assert result.exit_code == 0
        assert "unclaimed" in result.stdout

    def test_release_only_clears_own_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="other-session")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "me")
        result = runner.invoke(loop_app, ["release"])

        assert result.exit_code == 0
        assert "nothing released" in result.stdout
        assert LoopLease.objects.get(name="loop-owner").session_id == "other-session"

    def test_release_clears_when_holder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "me")
        LoopLease.objects.claim_ownership("loop-owner", session_id="me")
        result = runner.invoke(loop_app, ["release"])

        assert result.exit_code == 0
        assert "released loop slot" in result.stdout
        assert LoopLease.objects.get(name="loop-owner").session_id == ""

    def test_take_over_seizes_live_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="hijacker")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "main")
        result = runner.invoke(loop_app, ["claim", "--take-over"])

        assert result.exit_code == 0
        assert "claimed loop slot" in result.stdout
        assert LoopLease.objects.get(name="loop-owner").session_id == "main"

    def test_claim_without_take_over_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="hijacker")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "main")
        result = runner.invoke(loop_app, ["claim"])

        assert result.exit_code == 0
        assert "held by session hijacker" in result.stdout
        assert LoopLease.objects.get(name="loop-owner").session_id == "hijacker"

    def test_owner_json_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="json-sess")
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

    def test_claim_json_success_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "json-claimer")
        result = runner.invoke(loop_app, ["claim", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload == {"ok": True, "slot": "loop-owner", "owner_session": "json-claimer"}

    def test_claim_json_no_session_id_error_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json  # noqa: PLC0415

        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("T3_LOOP_SESSION_ID", raising=False)
        result = runner.invoke(loop_app, ["claim", "--json"])

        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "without a Claude session id" in payload["error"]

    def test_release_json_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        monkeypatch.setenv("CLAUDE_SESSION_ID", "rel-sess")
        LoopLease.objects.claim_ownership("loop-owner", session_id="rel-sess")
        result = runner.invoke(loop_app, ["release", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"ok": True, "slot": "loop-owner"}
