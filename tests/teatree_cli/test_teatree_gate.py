"""``t3 <overlay> gate status|disable|enable`` — the self-rescue command (#1474).

The gate subgroup is the orchestrator's guaranteed escape from a heavy-Bash
lockout: it flips the durable ``[teatree] orchestrator_bash_gate_enabled``
kill-switch in ``~/.teatree.toml``. These tests drive the command through the
real overlay Typer app (the same surface ``t3 <overlay> gate …`` hits) against
a tmp ``~/.teatree.toml`` and assert the on-disk effect — no mocking of the
config layer, because the on-disk write IS the behaviour under test.
"""

from pathlib import Path

import pytest
import tomlkit
import typer
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder
from teatree.cli.teatree_gate import (
    COMPLETION_CLAIM_GATE_KEY,
    CONFIG_OVERWRITE_GATE_KEY,
    GATE_KEY,
    completion_claim_gate_is_enabled,
    config_overwrite_gate_is_enabled,
    gate_is_enabled,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def app() -> typer.Typer:
    return OverlayAppBuilder(overlay_name="acme", project_path=None).build()


def _gate_value(home: Path) -> object:
    return tomlkit.parse((home / ".teatree.toml").read_text(encoding="utf-8"))["teatree"][GATE_KEY]


class TestGateStatus:
    def test_status_reports_enabled_when_config_missing(self, app: typer.Typer, home: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "status"])
        assert result.exit_code == 0, result.output
        assert "ENABLED" in result.output

    def test_status_reports_disabled_after_disable(self, app: typer.Typer, home: Path) -> None:
        (home / ".teatree.toml").write_text(f"[teatree]\n{GATE_KEY} = false\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["gate", "status"])
        assert result.exit_code == 0, result.output
        assert "DISABLED" in result.output


class TestGateDisableEnable:
    def test_disable_writes_false(self, app: typer.Typer, home: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "disable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(home) is False
        assert gate_is_enabled() is False

    def test_enable_writes_true(self, app: typer.Typer, home: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "enable"])
        assert result.exit_code == 0, result.output
        assert _gate_value(home) is True
        assert gate_is_enabled() is True

    def test_disable_then_enable_round_trips(self, app: typer.Typer, home: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "disable"]).exit_code == 0
        assert gate_is_enabled() is False
        assert runner.invoke(app, ["gate", "enable"]).exit_code == 0
        assert gate_is_enabled() is True


class TestGateIsEnabledFailsOpen:
    """``gate_is_enabled`` fails OPEN so the reported status matches the gate."""

    def test_enabled_on_broken_toml(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("this is not = valid = toml [[[", encoding="utf-8")
        assert gate_is_enabled() is True

    def test_enabled_when_teatree_not_a_table(self, home: Path) -> None:
        (home / ".teatree.toml").write_text('teatree = "oops"\n', encoding="utf-8")
        assert gate_is_enabled() is True


class TestConfigOverwriteGate:
    """``t3 <overlay> gate config-overwrite disable|enable`` — the PR #2661 self-rescue."""

    def test_disable_writes_false_and_is_reflected(self, app: typer.Typer, home: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "config-overwrite", "disable"])
        assert result.exit_code == 0, result.output
        document = tomlkit.parse((home / ".teatree.toml").read_text(encoding="utf-8"))
        assert document["teatree"][CONFIG_OVERWRITE_GATE_KEY] is False
        assert config_overwrite_gate_is_enabled() is False

    def test_enabled_by_default_when_config_missing(self, home: Path) -> None:
        assert config_overwrite_gate_is_enabled() is True

    def test_round_trips(self, app: typer.Typer, home: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "config-overwrite", "disable"]).exit_code == 0
        assert config_overwrite_gate_is_enabled() is False
        assert runner.invoke(app, ["gate", "config-overwrite", "enable"]).exit_code == 0
        assert config_overwrite_gate_is_enabled() is True


class TestCompletionClaimGate:
    """``t3 <overlay> gate completion-claim disable|enable`` — the #2665 self-rescue."""

    def test_disable_writes_false_and_is_reflected(self, app: typer.Typer, home: Path) -> None:
        result = CliRunner().invoke(app, ["gate", "completion-claim", "disable"])
        assert result.exit_code == 0, result.output
        document = tomlkit.parse((home / ".teatree.toml").read_text(encoding="utf-8"))
        assert document["teatree"][COMPLETION_CLAIM_GATE_KEY] is False
        assert completion_claim_gate_is_enabled() is False

    def test_enabled_by_default_when_config_missing(self, home: Path) -> None:
        assert completion_claim_gate_is_enabled() is True

    def test_round_trips(self, app: typer.Typer, home: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(app, ["gate", "completion-claim", "disable"]).exit_code == 0
        assert completion_claim_gate_is_enabled() is False
        assert runner.invoke(app, ["gate", "completion-claim", "enable"]).exit_code == 0
        assert completion_claim_gate_is_enabled() is True


class TestTomlPreservation:
    def test_disable_preserves_other_content(self, app: typer.Typer, home: Path) -> None:
        (home / ".teatree.toml").write_text(
            '# keep me\n[teatree]\nmode = "auto"\n\n[overlays.acme]\nmessaging_backend = "slack"\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(app, ["gate", "disable"])
        assert result.exit_code == 0, result.output
        document = tomlkit.parse((home / ".teatree.toml").read_text(encoding="utf-8"))
        assert document["teatree"]["mode"] == "auto"
        assert document["teatree"][GATE_KEY] is False
        assert document["overlays"]["acme"]["messaging_backend"] == "slack"
        assert "# keep me" in (home / ".teatree.toml").read_text(encoding="utf-8")
