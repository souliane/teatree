"""``t3 review gate fail-open enable|disable|status`` — the master fail-open switch.

The over-deny gates (quote-scanner / banned-terms on a PRIVATE surface,
validate-mr broken-env, skill-loading, protect-default-branch,
block-uncovered-diff, agent-plan-gate) can wedge the factory when their
detection misbehaves. ``danger_gate_fail_open`` is the master switch that
flips ALL of them to fail-open at once. It is OFF by default — the gates
keep their protective posture unless the operator deliberately turns the
escape hatch on. The ``danger_`` prefix makes a forgotten ``true`` override
in ``~/.teatree.toml`` unmissable.

These tests drive the command through the real ``review`` Typer app (the
same surface ``t3 review gate fail-open …`` hits) against a tmp
``~/.teatree.toml`` and assert the on-disk effect — no mocking of the
config layer, because the on-disk write IS the behaviour under test.
"""

from pathlib import Path

import pytest
import tomlkit
from typer.testing import CliRunner

from teatree.cli.review import review_app
from teatree.cli.teatree_gate import DANGER_GATE_FAIL_OPEN_KEY, danger_gate_fail_open_is_enabled


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _fail_open_value(home: Path) -> object:
    return tomlkit.parse((home / ".teatree.toml").read_text(encoding="utf-8"))["teatree"][DANGER_GATE_FAIL_OPEN_KEY]


class TestDangerPrefixedKey:
    def test_key_is_danger_prefixed(self) -> None:
        assert DANGER_GATE_FAIL_OPEN_KEY == "danger_gate_fail_open"

    def test_enable_writes_the_danger_prefixed_key(self, home: Path) -> None:
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "enable"])
        assert result.exit_code == 0, result.output
        document = tomlkit.parse((home / ".teatree.toml").read_text(encoding="utf-8"))
        assert "danger_gate_fail_open" in document["teatree"]
        assert "gate_fail_open" not in {k for k in document["teatree"] if k != "danger_gate_fail_open"}


class TestDefaultOff:
    def test_disabled_when_config_missing(self, home: Path) -> None:
        assert danger_gate_fail_open_is_enabled() is False

    def test_disabled_when_key_absent(self, home: Path) -> None:
        (home / ".teatree.toml").write_text('[teatree]\nmode = "auto"\n', encoding="utf-8")
        assert danger_gate_fail_open_is_enabled() is False

    def test_status_reports_off_by_default(self, home: Path) -> None:
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "status"])
        assert result.exit_code == 0, result.output
        assert "fail-open OFF" in result.output


class TestEnableDisable:
    def test_enable_writes_true_and_resolver_agrees(self, home: Path) -> None:
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "enable"])
        assert result.exit_code == 0, result.output
        assert _fail_open_value(home) is True
        assert danger_gate_fail_open_is_enabled() is True

    def test_disable_writes_false_and_resolver_agrees(self, home: Path) -> None:
        (home / ".teatree.toml").write_text(f"[teatree]\n{DANGER_GATE_FAIL_OPEN_KEY} = true\n", encoding="utf-8")
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "disable"])
        assert result.exit_code == 0, result.output
        assert _fail_open_value(home) is False
        assert danger_gate_fail_open_is_enabled() is False

    def test_enable_then_disable_round_trips(self, home: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(review_app, ["gate", "fail-open", "enable"]).exit_code == 0
        assert danger_gate_fail_open_is_enabled() is True
        assert runner.invoke(review_app, ["gate", "fail-open", "disable"]).exit_code == 0
        assert danger_gate_fail_open_is_enabled() is False

    def test_status_reports_on_after_enable(self, home: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(review_app, ["gate", "fail-open", "enable"]).exit_code == 0
        result = runner.invoke(review_app, ["gate", "fail-open", "status"])
        assert result.exit_code == 0, result.output
        assert "fail-open ON" in result.output


class TestResolverFailsClosed:
    """The master switch is OFF unless an explicit ``true`` is recorded.

    Unlike the kill-switch keys (which fail OPEN to enabled), the fail-open
    master switch fails CLOSED to disabled on a broken/odd config — a
    parse error must never silently relax every gate.
    """

    def test_off_on_broken_toml(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("this is not = valid = toml [[[", encoding="utf-8")
        assert danger_gate_fail_open_is_enabled() is False

    def test_off_when_teatree_not_a_table(self, home: Path) -> None:
        (home / ".teatree.toml").write_text('teatree = "oops"\n', encoding="utf-8")
        assert danger_gate_fail_open_is_enabled() is False

    def test_off_when_only_old_key_present(self, home: Path) -> None:
        (home / ".teatree.toml").write_text("[teatree]\ngate_fail_open = true\n", encoding="utf-8")
        assert danger_gate_fail_open_is_enabled() is False


class TestTomlPreservation:
    def test_enable_preserves_other_content(self, home: Path) -> None:
        (home / ".teatree.toml").write_text(
            '# keep me\n[teatree]\nmode = "auto"\n\n[overlays.acme]\nmessaging_backend = "slack"\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(review_app, ["gate", "fail-open", "enable"])
        assert result.exit_code == 0, result.output
        document = tomlkit.parse((home / ".teatree.toml").read_text(encoding="utf-8"))
        assert document["teatree"]["mode"] == "auto"
        assert document["teatree"][DANGER_GATE_FAIL_OPEN_KEY] is True
        assert document["overlays"]["acme"]["messaging_backend"] == "slack"
