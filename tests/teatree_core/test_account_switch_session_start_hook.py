"""SessionStart `/login` account-switch advisory (#1916).

The advisory rides the single SessionStart stdout write via
``_merge_session_start_context``; it fires when the active account fingerprint
differs from the last-recovered one and stays silent otherwise. The pure
fingerprint reader is exercised against a synthetic ``~/.claude.json`` under a
patched ``Path.home``.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.core.account_fingerprint import record_fingerprint


def _write_active_account(home: Path, account_uuid: str) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": account_uuid}}),
        encoding="utf-8",
    )


@pytest.fixture
def staged_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class TestAccountSwitchAdvisory:
    def test_no_record_no_advisory(self, staged_home: Path) -> None:
        _write_active_account(staged_home, "uuid-A")
        assert router._account_switch_advisory() is None

    def test_same_account_no_advisory(self, staged_home: Path) -> None:
        _write_active_account(staged_home, "uuid-A")
        record_fingerprint("uuid-A", home=staged_home)
        assert router._account_switch_advisory() is None

    def test_switch_emits_advisory(self, staged_home: Path) -> None:
        _write_active_account(staged_home, "uuid-B")
        record_fingerprint("uuid-A", home=staged_home)
        advisory = router._account_switch_advisory()
        assert advisory is not None
        assert "account switch detected" in advisory.lower()
        assert "t3 doctor check" in advisory

    def test_merge_prepends_advisory_to_session_context(self, staged_home: Path) -> None:
        _write_active_account(staged_home, "uuid-B")
        record_fingerprint("uuid-A", home=staged_home)
        merged = router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup")
        assert "BASE DIRECTIVE" in merged
        assert merged.index("t3 doctor check") < merged.index("BASE DIRECTIVE")

    def test_merge_no_switch_leaves_context_unchanged(self, staged_home: Path) -> None:
        _write_active_account(staged_home, "uuid-A")
        record_fingerprint("uuid-A", home=staged_home)
        merged = router._merge_session_start_context("BASE DIRECTIVE", "sess-1", "startup")
        assert "account switch detected" not in merged.lower()
