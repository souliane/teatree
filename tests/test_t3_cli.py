"""Tests for the t3 CLI."""

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import typer
from conftest import load_script
from lib.registry import clear as ep_clear
from lib.registry import register as ep_register
from typer import Typer as TyperApp
from typer.testing import CliRunner

if TYPE_CHECKING:
    import types

runner = CliRunner()


@pytest.fixture
def ticket_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a minimal ticket dir environment."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))

    main = ws / "my-project"
    main.mkdir()
    (main / ".git").mkdir()

    td = ws / "ac-1234"
    td.mkdir()
    wt = td / "my-project"
    wt.mkdir()

    monkeypatch.setenv("TICKET_DIR", str(td))
    monkeypatch.setenv("_T3_ORIG_CWD", str(wt))
    return td


@pytest.fixture
def cli_app() -> "typer.Typer":
    """Load the t3_cli module and return its app."""
    mod = load_script("t3_cli")
    return mod.app


class TestStatusCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_status_json_output(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["lifecycle", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["state"] == "created"
        assert "available_transitions" in data

    @pytest.mark.usefixtures("ticket_env")
    def test_status_human_output(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["lifecycle", "status"])
        assert result.exit_code == 0
        assert "State: created" in result.stdout
        assert "Available transitions:" in result.stdout
        assert "provision" in result.stdout

    def test_status_shows_ports_when_provisioned(self, ticket_env: Path, cli_app: "typer.Typer") -> None:
        state_file = ticket_env / ".state.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "provisioned",
                    "facts": {
                        "ports": {"backend": 8005, "frontend": 4205, "postgres": 5437, "redis": 6379},
                        "db_name": "wt_1234_acme",
                    },
                }
            )
        )
        result = runner.invoke(cli_app, ["lifecycle", "status"])
        assert result.exit_code == 0
        assert "http://localhost:8005" in result.stdout
        assert "wt_1234_acme" in result.stdout

    def test_status_no_ticket_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_app: "typer.Typer"
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path))
        monkeypatch.delenv("TICKET_DIR", raising=False)
        monkeypatch.setenv("_T3_ORIG_CWD", str(tmp_path))
        result = runner.invoke(cli_app, ["lifecycle", "status"])
        assert result.exit_code == 1


class TestDiagramCommand:
    def test_diagram_outputs_mermaid(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["lifecycle", "diagram"])
        assert result.exit_code == 0
        assert "stateDiagram-v2" in result.stdout
        assert "created --> provisioned" in result.stdout


class TestCleanCommand:
    def test_clean_resets_state(self, ticket_env: Path, cli_app: "typer.Typer") -> None:
        state_file = ticket_env / ".state.json"
        state_file.write_text(json.dumps({"state": "provisioned", "facts": {"db_name": "test"}}))
        result = runner.invoke(cli_app, ["lifecycle", "clean"])
        assert result.exit_code == 0
        assert "Worktree cleaned" in result.stdout
        assert not state_file.is_file()


class TestSetupCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_setup_provisions_worktree(self, cli_app: "typer.Typer") -> None:
        with (
            patch("lib.lifecycle.db_exists", return_value=True),
            patch("lib.lifecycle.find_free_ports", return_value=(8001, 4201, 5433, 6379)),
            patch("lib.lifecycle.registry") as mock_reg,
        ):
            mock_reg.call.return_value = True
            result = runner.invoke(cli_app, ["lifecycle", "setup"])
            assert result.exit_code == 0
            data = json.loads(result.stdout)
            assert data["state"] == "provisioned"


class TestStartCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_start_requires_provisioned(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["lifecycle", "start"])
        assert result.exit_code != 0

    def test_start_from_provisioned(self, ticket_env: Path, cli_app: "typer.Typer") -> None:
        state_file = ticket_env / ".state.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "provisioned",
                    "facts": {"wt_dir": "/wt", "ports": {"backend": 8001, "frontend": 4201}},
                }
            )
        )
        with patch("lib.lifecycle.registry"):
            result = runner.invoke(cli_app, ["lifecycle", "start"])
            assert result.exit_code == 0
            data = json.loads(result.stdout)
            assert data["state"] == "ready"


class TestDbGroup:
    @pytest.mark.usefixtures("ticket_env")
    def test_db_refresh_requires_provisioned(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["db", "refresh"])
        assert result.exit_code != 0

    def test_db_refresh_from_provisioned(self, ticket_env: Path, cli_app: "typer.Typer") -> None:
        state_file = ticket_env / ".state.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "provisioned",
                    "facts": {"db_name": "wt_1234", "variant": "", "main_repo": "/repo", "wt_dir": "/wt"},
                }
            )
        )
        with patch("lib.lifecycle.registry"):
            result = runner.invoke(cli_app, ["db", "refresh"])
            assert result.exit_code == 0
            data = json.loads(result.stdout)
            assert data["state"] == "provisioned"


class TestGroupsRegistered:
    """Verify all groups and their commands are discoverable."""

    @pytest.mark.parametrize(
        "group",
        ["lifecycle", "workspace", "run", "ci", "db", "mr"],
    )
    def test_group_exists(self, cli_app: "typer.Typer", group: str) -> None:
        result = runner.invoke(cli_app, [group, "--help"])
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        ("group", "commands"),
        [
            ("lifecycle", ["status", "diagram", "setup", "start", "clean"]),
            ("workspace", ["ticket", "finalize", "clean-all"]),
            ("run", ["backend", "frontend", "build-frontend", "tests", "verify"]),
            ("ci", ["cancel", "trigger-e2e", "fetch-errors", "fetch-failed-tests", "quality-check"]),
            ("db", ["refresh", "restore-ci", "reset-passwords"]),
            ("mr", ["create", "check-gates", "fetch-issue", "detect-tenant", "followup"]),
        ],
    )
    def test_group_commands(self, cli_app: "typer.Typer", group: str, commands: list[str]) -> None:
        result = runner.invoke(cli_app, [group, "--help"])
        assert result.exit_code == 0
        for cmd in commands:
            assert cmd in result.stdout, f"'{cmd}' not in `t3 {group} --help` output"


class TestInfoCommand:
    def test_info_json_output(self, cli_app: "typer.Typer") -> None:
        ep_register("wt_run_backend", lambda: None, "default")
        result = runner.invoke(cli_app, ["info", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert any(d["point"] == "wt_run_backend" for d in data)

    def test_info_human_output(self, cli_app: "typer.Typer") -> None:
        ep_register("wt_run_backend", lambda: None, "default")
        result = runner.invoke(cli_app, ["info"])
        assert result.exit_code == 0
        assert "Extension Point" in result.stdout
        assert "wt_run_backend" in result.stdout
        assert "default" in result.stdout

    def test_info_shows_layer_for_overridden(self, cli_app: "typer.Typer") -> None:
        ep_register("wt_run_backend", lambda: None, "default")
        ep_register("wt_run_backend", lambda: None, "project")
        result = runner.invoke(cli_app, ["info"])
        assert result.exit_code == 0
        assert "project" in result.stdout

    def test_info_empty_registry(self, cli_app: "typer.Typer") -> None:
        ep_clear()
        result = runner.invoke(cli_app, ["info"])
        assert result.exit_code == 0
        assert "No extension points registered" in result.stdout

    def test_info_appears_in_top_level_help(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["--help"])
        assert result.exit_code == 0
        assert "info" in result.stdout


class TestConfigAutoload:
    def test_no_context_match_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "my-skill").mkdir()
        monkeypatch.setattr("lib.env._SKILL_ROOTS", (str(skills_dir),))
        mod = load_script("t3_cli")
        result = runner.invoke(mod.app, ["config", "autoload"])
        assert result.exit_code == 0
        assert "No context-match.yml" in result.stdout

    def test_empty_context_match_no_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "empty-overlay"
        (skill / "hook-config").mkdir(parents=True)
        (skill / "hook-config" / "context-match.yml").write_text("# only comments\n")
        monkeypatch.setattr("lib.env._SKILL_ROOTS", (str(skills_dir),))
        mod = load_script("t3_cli")
        result = runner.invoke(mod.app, ["config", "autoload"])
        assert result.exit_code == 0
        assert "empty-overlay" in result.stdout
        # No table rendered (no rows)
        assert "Type" not in result.stdout

    def test_displays_rules_from_context_match(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "my-overlay"
        (skill / "hook-config").mkdir(parents=True)
        (skill / "hook-config" / "context-match.yml").write_text(
            '# A comment\n\ncwd_patterns:\n  - "my-repo"\n'
            'companion_skills:\n  ac-python:\n    - "my-repo"\n'
            "unknown_section:\n  ignored: true\n"
        )
        monkeypatch.setattr("lib.env._SKILL_ROOTS", (str(skills_dir),))
        mod = load_script("t3_cli")
        result = runner.invoke(mod.app, ["config", "autoload"])
        assert result.exit_code == 0
        assert "my-overlay" in result.stdout
        assert "my-repo" in result.stdout
        assert "ac-python" in result.stdout


class TestFullStatusCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_full_status_human_output(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["full-status"])
        assert result.exit_code == 0
        assert "worktree:" in result.stdout
        assert "ticket:" in result.stdout
        assert "session:" in result.stdout

    @pytest.mark.usefixtures("ticket_env")
    def test_full_status_json_output(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["full-status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "worktree" in data
        assert "ticket" in data
        assert "session" in data

    def test_full_status_no_ticket_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_app: "typer.Typer"
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path))
        monkeypatch.delenv("TICKET_DIR", raising=False)
        monkeypatch.setenv("_T3_ORIG_CWD", str(tmp_path))
        result = runner.invoke(cli_app, ["full-status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["worktree"] is None
        assert data["ticket"] is None


class TestStartTicketCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_start_ticket_chains_steps(self) -> None:
        mod = load_script("t3_cli")
        calls: list[str] = []
        original_ep_call = mod.ep_call

        def fake_ep_call(name: str, *_args: object, **_kwargs: object) -> None:
            calls.append(name)

        mod.ep_call = fake_ep_call  # type: ignore[attr-defined]
        try:
            with (
                patch("lib.lifecycle.db_exists", return_value=True),
                patch("lib.lifecycle.find_free_ports", return_value=(8001, 4201, 5433, 6379)),
                patch("lib.lifecycle.registry") as mock_reg,
            ):
                mock_reg.call.return_value = True
                result = runner.invoke(mod.app, ["start-ticket", "https://example.com/issue/1"])
        finally:
            mod.ep_call = original_ep_call  # type: ignore[attr-defined]
        assert result.exit_code == 0
        assert "fetch_issue_context" in calls
        assert "ws_create_ticket_worktree" in calls

    def test_start_ticket_skips_already_ready(self, ticket_env: Path) -> None:
        """When lifecycle is already ready and ticket already started, skip transitions."""
        # Set lifecycle to ready state
        state_file = ticket_env / ".state.json"
        state_file.write_text(
            json.dumps(
                {
                    "state": "ready",
                    "facts": {
                        "wt_dir": "/wt",
                        "main_repo": "/repo",
                        "ports": {"backend": 8001, "frontend": 4201, "postgres": 5433, "redis": 6379},
                    },
                }
            )
        )
        # Set ticket to started state
        ticket_file = ticket_env / "ticket.json"
        ticket_file.write_text(json.dumps({"state": "started", "facts": {"issue_url": "x", "worktree_dirs": ["/wt"]}}))

        mod = load_script("t3_cli")
        calls: list[str] = []
        original_ep_call = mod.ep_call

        def fake_ep_call(name: str, *_args: object, **_kwargs: object) -> None:
            calls.append(name)

        mod.ep_call = fake_ep_call  # type: ignore[attr-defined]
        try:
            result = runner.invoke(mod.app, ["start-ticket", "https://example.com/issue/1"])
        finally:
            mod.ep_call = original_ep_call  # type: ignore[attr-defined]
        assert result.exit_code == 0
        # Lifecycle and ticket transitions were skipped — verify via persisted state
        lc_data = json.loads(state_file.read_text())
        assert lc_data["state"] == "ready"
        tk_data = json.loads(ticket_file.read_text())
        assert tk_data["state"] == "started"


class TestShipCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_ship_blocked_without_testing(self, cli_app: "typer.Typer") -> None:
        result = runner.invoke(cli_app, ["ship"])
        assert result.exit_code == 1
        assert "testing" in result.stdout

    @pytest.mark.usefixtures("ticket_env")
    def test_ship_blocked_without_reviewing(self) -> None:
        mod = load_script("t3_cli")
        # Simulate a session that has visited testing but not reviewing
        with patch.object(mod, "_get_session") as mock_session:
            session = MagicMock()
            session.has_visited.side_effect = lambda p: p == "testing"
            mock_session.return_value = session
            result = runner.invoke(mod.app, ["ship"])
        assert result.exit_code == 1
        assert "reviewing" in result.stdout

    @pytest.mark.usefixtures("ticket_env")
    def test_ship_force_bypasses_gates(self) -> None:
        mod = load_script("t3_cli")
        calls: list[str] = []
        original_ep_call = mod.ep_call

        def fake_ep_call(name: str, *_args: object, **_kwargs: object) -> None:
            calls.append(name)

        mod.ep_call = fake_ep_call  # type: ignore[attr-defined]
        try:
            result = runner.invoke(mod.app, ["ship", "--force"])
        finally:
            mod.ep_call = original_ep_call  # type: ignore[attr-defined]
        assert result.exit_code == 0
        assert "wt_cancel_stale_pipelines" in calls
        assert "wt_push" in calls
        assert "wt_create_mr" in calls


class TestDailyCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_daily_chains_steps(self) -> None:
        mod = load_script("t3_cli")
        calls: list[str] = []
        original_ep_call = mod.ep_call

        def fake_ep_call(name: str, *_args: object, **_kwargs: object) -> None:
            calls.append(name)

        mod.ep_call = fake_ep_call  # type: ignore[attr-defined]
        try:
            result = runner.invoke(mod.app, ["daily"])
        finally:
            mod.ep_call = original_ep_call  # type: ignore[attr-defined]
        assert result.exit_code == 0
        assert "followup_collect" in calls
        assert "followup_check_gates" in calls
        assert "followup_remind_reviewers" in calls


class TestPostEvidenceCommand:
    @pytest.mark.usefixtures("ticket_env")
    def test_post_evidence_delegates(self) -> None:
        mod = load_script("t3_cli")
        calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        original_ep_call = mod.ep_call

        def fake_ep_call(name: str, *_args: object, **_kwargs: object) -> None:
            calls.append((name, _args, _kwargs))

        mod.ep_call = fake_ep_call  # type: ignore[attr-defined]
        try:
            result = runner.invoke(mod.app, ["mr", "post-evidence", "img.png"])
        finally:
            mod.ep_call = original_ep_call  # type: ignore[attr-defined]
        assert result.exit_code == 0
        assert calls[0][0] == "wt_post_mr_evidence"


class TestGetTicketErrorPath:
    def test_get_ticket_no_ticket_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = load_script("t3_cli")
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path))
        monkeypatch.delenv("TICKET_DIR", raising=False)
        monkeypatch.setenv("_T3_ORIG_CWD", str(tmp_path))
        with pytest.raises(typer.Exit):
            mod._get_ticket()


class TestSetupWithStart:
    @pytest.mark.usefixtures("ticket_env")
    def test_setup_start_provisions_and_starts(self, cli_app: "typer.Typer") -> None:
        with (
            patch("lib.lifecycle.db_exists", return_value=True),
            patch("lib.lifecycle.find_free_ports", return_value=(8001, 4201, 5433, 6379)),
            patch("lib.lifecycle.registry") as mock_reg,
        ):
            mock_reg.call.return_value = True
            result = runner.invoke(cli_app, ["lifecycle", "setup", "--start"])
            assert result.exit_code == 0
            data = json.loads(result.stdout)
            assert data["state"] == "ready"


class TestShipWithReviewedTicket:
    def test_ship_transitions_reviewed_ticket(self, ticket_env: Path) -> None:
        # Set ticket to reviewed state
        ticket_file = ticket_env / "ticket.json"
        ticket_file.write_text(json.dumps({"state": "reviewed", "facts": {"issue_url": "x"}}))

        mod = load_script("t3_cli")
        calls: list[str] = []
        original_ep_call = mod.ep_call

        def fake_ep_call(name: str, *_args: object, **_kwargs: object) -> None:
            calls.append(name)

        mod.ep_call = fake_ep_call  # type: ignore[attr-defined]
        try:
            result = runner.invoke(mod.app, ["ship", "--force"])
        finally:
            mod.ep_call = original_ep_call  # type: ignore[attr-defined]
        assert result.exit_code == 0
        # Verify ticket state was updated
        data = json.loads(ticket_file.read_text())
        assert data["state"] == "shipped"


class TestStartAutoProvision:
    @pytest.mark.usefixtures("ticket_env")
    def test_start_auto_provisions_from_created(self, cli_app: "typer.Typer") -> None:
        """When state is 'created', start should auto-provision first."""
        with (
            patch("lib.lifecycle.db_exists", return_value=True),
            patch("lib.lifecycle.find_free_ports", return_value=(8001, 4201, 5433, 6379)),
            patch("lib.lifecycle.registry") as mock_reg,
        ):
            mock_reg.call.return_value = True
            result = runner.invoke(cli_app, ["lifecycle", "start"])
            assert result.exit_code == 0
            data = json.loads(result.stdout)
            assert data["state"] == "ready"


class TestExtensionPointDelegates:
    @pytest.fixture
    def cli_mod(self) -> "types.ModuleType":
        return load_script("t3_cli")

    @pytest.mark.parametrize(
        ("args", "extension_point"),
        [
            (["run", "backend"], "wt_run_backend"),
            (["run", "frontend"], "wt_run_frontend"),
            (["run", "build-frontend"], "wt_build_frontend"),
            (["run", "tests"], "wt_run_tests"),
            (["ci", "trigger-e2e"], "wt_trigger_e2e"),
            (["ci", "fetch-errors"], "wt_fetch_ci_errors"),
            (["ci", "fetch-failed-tests"], "wt_fetch_failed_tests"),
            (["ci", "quality-check"], "wt_quality_check"),
            (["db", "restore-ci"], "wt_restore_ci_db"),
            (["db", "reset-passwords"], "wt_reset_passwords"),
        ],
    )
    def test_delegate_calls_extension_point(
        self, cli_mod: "types.ModuleType", args: list[str], extension_point: str
    ) -> None:
        mock_call = MagicMock()
        original = cli_mod.ep_call
        cli_mod.ep_call = mock_call  # type: ignore[attr-defined]
        try:
            result = runner.invoke(cli_mod.app, args)
            assert result.exit_code == 0
            mock_call.assert_called_once_with(extension_point)
        finally:
            cli_mod.ep_call = original  # type: ignore[attr-defined]


class TestOverlayRegistration:
    def test_overlay_registers_group(self) -> None:
        """Verify that create_cli_group adds a named sub-app."""
        mod = load_script("t3_cli")
        sub_app = TyperApp()
        mock_hooks = MagicMock(create_cli_group=MagicMock(return_value=("test-overlay", "Test commands", sub_app)))

        with patch.dict("sys.modules", {"lib.project_hooks": mock_hooks}):
            mod._register_overlay_commands()
            mock_hooks.create_cli_group.assert_called_once()

        result = runner.invoke(mod.app, ["test-overlay", "--help"])
        assert result.exit_code == 0

    def test_overlay_tags_extension_point_commands(self) -> None:
        """When an EP has a project-layer override, its help text gets tagged."""
        ep_register("wt_run_backend", lambda: None, "project")

        mod = load_script("t3_cli")
        mod._tag_overlay_commands("myproject")

        # Check the help text was modified
        for cmd in mod.run_app.registered_commands:
            resolved = cmd.name or (getattr(cmd.callback, "__name__", "").replace("_", "-") if cmd.callback else "")
            if resolved == "backend":
                assert "[myproject]" in (cmd.help or "")

    def test_overlay_import_error_is_silent(self) -> None:
        """When lib.project_hooks is not importable, no overlay group is added."""
        mod = load_script("t3_cli")
        with patch.dict("sys.modules", {"lib.project_hooks": None}):
            mod._register_overlay_commands()  # should not raise
