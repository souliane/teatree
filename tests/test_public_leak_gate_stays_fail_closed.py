"""HARD INVARIANT: the PUBLIC-egress leak gate stays fail-CLOSED always.

The master ``danger_gate_fail_open`` switch (NEVER-LOCKOUT) flips the OVER-DENY
gates (skill-loading, protect-default-branch, validate-mr broken-env,
block-uncovered-diff, agent-plan-gate) to fail-open. It must NEVER relax
the PUBLIC-egress leak gate — the quote-scanner / banned-terms deny on a
PUBLIC surface and the ``publish_surface`` carve-out. Relaxing a public
leak block is a privacy regression, not a lockout rescue.

These regression tests assert that with ``danger_gate_fail_open = true``
recorded as a DB-home ``ConfigSetting`` row, a public-surface quote/banned
match STILL denies — proving the leak path never consults the master switch.
"""

import json
import sqlite3
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_banned_terms_pretool, handle_quote_scanner_pretool
from teatree.hooks import _repo_visibility


def _seed_config_db(path: Path, rows: dict[str, object]) -> None:
    conn = sqlite3.connect(str(path))
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
    conn.close()


@pytest.fixture
def fail_open_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record ``danger_gate_fail_open = true`` as a DB-home ``ConfigSetting`` row.

    Also pins the quote-scanner ledger root under ``tmp_path`` so the gate
    decision does not touch real state, and CONFIRMS the genuinely-public
    ``souliane/teatree`` target public so the leak gate (which scopes to an
    affirmatively-public destination, #1415/#1213) actually reaches its deny.
    """
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    config_db = tmp_path / "config.sqlite3"
    _seed_config_db(config_db, {"danger_gate_fail_open": True, "banned_terms": ["acmecorp"]})
    monkeypatch.setenv("T3_CONFIG_DB", str(config_db))
    return tmp_path


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestQuoteScannerLeakGateIgnoresFailOpen:
    def test_public_quote_leak_still_denies_with_fail_open_on(
        self, fail_open_on: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A PUBLIC posting surface (gh pr create) carrying a verbatim
        # user-quote pattern. Even with the master fail-open switch ON, the
        # leak gate must DENY.
        data = _bash('gh pr create --repo souliane/teatree --title t --body "## User ask (verbatim)\nplease ship now"')
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is True, "PUBLIC quote leak must stay fail-closed even with danger_gate_fail_open=true"
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "quote-scanner" in decision["permissionDecisionReason"]


class TestBannedTermsLeakGateIgnoresFailOpen:
    def test_public_banned_term_still_denies_with_fail_open_on(
        self, fail_open_on: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A PUBLIC posting surface (gh issue create) carrying a banned
        # overlay/customer term. The leak gate must DENY despite fail-open.
        data = _bash('gh issue create --repo souliane/teatree --title t --body "rolling out acmecorp integration"')
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True, "PUBLIC banned-term leak must stay fail-closed even with danger_gate_fail_open=true"
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "banned-terms" in decision["permissionDecisionReason"]


class TestLeakGateNeverReadsFailOpen:
    """The leak handlers must not even CALL the master-switch resolver.

    A stricter guard than the behavioural tests above: the public-egress
    handlers run to a deny without ``_danger_gate_fail_open_enabled`` ever
    being consulted. If a future refactor routes the leak path through the
    shared ``_fail_open_or_deny`` (which reads the switch), this trips.
    """

    def test_quote_handler_does_not_consult_the_master_switch(
        self, fail_open_on: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[int] = []
        real = router._danger_gate_fail_open_enabled

        def _spy() -> bool:
            calls.append(1)
            return real()

        monkeypatch.setattr(router, "_danger_gate_fail_open_enabled", _spy)
        handle_quote_scanner_pretool(
            _bash('gh pr create --repo souliane/teatree --title t --body "## User ask (verbatim)\nship"')
        )
        capsys.readouterr()
        assert calls == [], "the PUBLIC leak gate must NEVER read danger_gate_fail_open"

    def test_banned_handler_does_not_consult_the_master_switch(
        self, fail_open_on: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[int] = []
        real = router._danger_gate_fail_open_enabled

        def _spy() -> bool:
            calls.append(1)
            return real()

        monkeypatch.setattr(router, "_danger_gate_fail_open_enabled", _spy)
        handle_banned_terms_pretool(
            _bash('gh issue create --repo souliane/teatree --title t --body "ship acmecorp now"')
        )
        capsys.readouterr()
        assert calls == [], "the PUBLIC leak gate must NEVER read danger_gate_fail_open"
