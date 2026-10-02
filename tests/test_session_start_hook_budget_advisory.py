# test-path: cross-cutting — tests a hooks/scripts sibling and its router wiring, which have no src/teatree/ mirror.
"""SessionStart advisory for an under-bounded hook in the operator's own settings.

``test_hooks_json_declare_timeouts.py`` bounds this repo's registrations and can reach
nothing else. The machine-local settings register hooks on the same chain, so the same
ceiling is applied there from the one venue that reads that file on the host — this
hook — riding the single SessionStart stdout write via ``_merge_session_start_context``.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.session_start_hook_budget import session_start_hook_budget_advisory


@pytest.fixture
def staged_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _write_settings(home: Path, hooks: list[dict]) -> None:
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": hooks}]}}), encoding="utf-8"
    )


class TestSessionStartHookBudgetAdvisory:
    def test_missing_settings_no_advisory(self, staged_home: Path) -> None:
        assert session_start_hook_budget_advisory() is None

    def test_bounded_registrations_no_advisory(self, staged_home: Path) -> None:
        _write_settings(staged_home, [{"command": "gate.sh", "timeout": 10}])

        assert session_start_hook_budget_advisory() is None

    def test_an_unbounded_registration_is_named(self, staged_home: Path) -> None:
        _write_settings(staged_home, [{"command": "t3-doctor-session-start.sh"}])

        advisory = session_start_hook_budget_advisory()

        assert advisory is not None
        assert "t3-doctor-session-start.sh" in advisory


class TestRidesTheOneSessionStartWrite:
    def test_merge_prepends_the_advisory_to_the_session_context(self, staged_home: Path) -> None:
        _write_settings(staged_home, [{"command": "t3-doctor-session-start.sh"}])

        merged = router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup")

        assert merged.index("t3-doctor-session-start.sh") < merged.index("BASE DIRECTIVE")

    def test_merge_leaves_a_bounded_chain_unchanged(self, staged_home: Path) -> None:
        _write_settings(staged_home, [{"command": "gate.sh", "timeout": 10}])

        assert router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup") == "BASE DIRECTIVE"
