"""The pre-publish leak gates enforce ONLY on affirmatively-public targets (#1415/#1213).

The banned-terms (#1415) and quote-scanner (#1213) PUBLISH gates protect against
leaking internal vocabulary / user quotes onto PUBLIC surfaces, so they enforce
ONLY when the target repository is affirmatively ``public``. For EVERY other case
-- a ``private``/``internal`` repo, or a target whose visibility cannot be
resolved in-hook (the common cold-hook state) -- the gate is SKIPPED entirely
(bias hard toward not firing: a non-public repo must never be falsely blocked).

These tests drive BOTH live handlers across the three-visibility matrix with the
forge visibility probe mocked (no ``gh``/``glab``, no network): a PUBLIC target
carrying a leak FIRES, and both a PRIVATE and an UNRESOLVABLE target carrying the
SAME leak SKIP. The public FIRE row is the anti-vacuity guard for the two SKIP
rows -- it proves they are green because the target is non-public, not because
the gate stopped detecting the leak. The unit block pins the polarity of the
:func:`public_visibility.gate_skips_for_visibility` predicate the handlers call.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool, handle_quote_scanner_pretool
from teatree.hooks import _repo_visibility, public_visibility
from teatree.hooks.publish_destination import resolve_publish_destination

_BANNED_TERM = "acmecorp"
_BANNED_LEAK = f"rolling out {_BANNED_TERM} integration"
_QUOTE_LEAK = "## User ask (verbatim, 2026-05-20)\nplease ship now"

# (handler, leak body, deny-reason token) for each leak gate under test.
_GATES = [
    pytest.param(handle_banned_terms_pretool, _BANNED_LEAK, "banned-terms", id="banned-terms-#1415"),
    pytest.param(handle_quote_scanner_pretool, _QUOTE_LEAK, "quote-scanner", id="quote-scanner-#1213"),
]

Handler = Callable[[dict], bool | None]


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate the probe cache (T3_DATA_DIR) and the config home so the unit block
    # (which does not request ``leak_home``) never reads the developer's real
    # config or a warm visibility cache. ``leak_home`` re-points both.
    home = tmp_path / "autohome"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))


@pytest.fixture
def leak_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed the banned-terms list in a DB-home config and isolate state.

    Isolates the probe cache + quote ledger under ``tmp_path`` (``T3_DATA_DIR``)
    so no test touches real state, and pins the banned-terms list for #1415.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
            (json.dumps([_BANNED_TERM]),),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return home


def _post(slug: str, body: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": f'gh issue create --repo {slug} --title t --body "{body}"'}}


@pytest.mark.usefixtures("leak_home")
@pytest.mark.parametrize(("handler", "leak_body", "reason_token"), _GATES)
def test_public_target_with_leak_fires(
    handler: Handler,
    leak_body: str,
    reason_token: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A leak toward an affirmatively-PUBLIC target (probe confirms public) is a
    # real public leak -> the gate DENIES. Anti-vacuity guard for the SKIP rows.
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    blocked = handler(_post("souliane/teatree", leak_body))
    decision = json.loads(capsys.readouterr().out)
    assert blocked is True
    assert decision["permissionDecision"] == "deny"
    assert reason_token in decision["permissionDecisionReason"]


@pytest.mark.usefixtures("leak_home")
@pytest.mark.parametrize(("handler", "leak_body", "reason_token"), _GATES)
def test_private_target_with_leak_skips(
    handler: Handler,
    leak_body: str,
    reason_token: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The SAME leak toward a probe-CONFIRMED-PRIVATE target is not a public leak
    # -> the gate SKIPS it entirely (no deny). A private repo is never blocked.
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PRIVATE")
    blocked = handler(_post("someowner/private-svc", leak_body))
    assert blocked is False
    assert capsys.readouterr().out == ""  # no deny JSON


@pytest.mark.usefixtures("leak_home")
@pytest.mark.parametrize(("handler", "leak_body", "reason_token"), _GATES)
def test_unresolvable_target_with_leak_skips(
    handler: Handler,
    leak_body: str,
    reason_token: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The SAME leak toward a target whose visibility the in-hook probe cannot
    # resolve (returns None -- the common cold-hook / lookup-failure state) is NOT
    # affirmatively public, so the gate SKIPS it (bias hard toward not firing).
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
    blocked = handler(_post("someowner/mystery", leak_body))
    assert blocked is False
    assert capsys.readouterr().out == ""  # no deny JSON


class TestGateSkipsForVisibilityPolarity:
    """The predicate both handlers call: SKIP unless the target is affirmatively public."""

    @staticmethod
    def _skips(command: str, monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> bool:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)
        return public_visibility.gate_skips_for_visibility(command, cwd=None)

    def test_confirmed_public_target_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._skips("gh issue create --repo souliane/teatree --body x", monkeypatch, "PUBLIC") is False

    def test_confirmed_private_target_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._skips("gh issue create --repo owner/private-svc --body x", monkeypatch, "PRIVATE") is True

    def test_unresolvable_target_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._skips("gh issue create --repo owner/mystery --body x", monkeypatch, None) is True

    def test_is_affirmatively_public_only_on_confirmed_public(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Distinct slugs so the per-slug day-cache does not carry the first
        # verdict into the second assertion.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        public_dest = resolve_publish_destination("gh issue create --repo souliane/teatree --body x")
        assert public_visibility.is_affirmatively_public(public_dest) is True
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        unknown_dest = resolve_publish_destination("gh issue create --repo owner/mystery --body x")
        assert public_visibility.is_affirmatively_public(unknown_dest) is False


class TestApiWriteUnresolvableDoesNotSkip:
    """A raw ``gh``/``glab api`` WRITE with an unresolvable endpoint must SCAN, not skip.

    A raw REST POST is an immediate public egress with no pre-push backstop, so
    the module contract makes an unresolvable / ``$``-carrying api WRITE
    non-skippable. The old ``return True`` treated an unresolvable endpoint as
    non-public (skip-eligible), routing a ``gh api "repos/$OWNER/repo/issues"``
    POST around the leak gate. A CONFIRMED-private api WRITE still skips.
    """

    @staticmethod
    def _skips(command: str, monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> bool:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)
        return public_visibility.gate_skips_for_visibility(command, cwd=None)

    def test_dollar_slug_api_write_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = 'gh api "repos/$OWNER/repo/issues" -f body=x'
        assert self._skips(cmd, monkeypatch, None) is False

    def test_flagless_unresolvable_api_write_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-repo / unresolvable endpoint on a WRITE method scans, never skips.
        cmd = "gh api graphql -f query=x --method POST"
        assert self._skips(cmd, monkeypatch, None) is False

    def test_confirmed_private_api_write_still_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = "glab api projects/owner%2Fprivate-svc/merge_requests/5 -X PUT -f description=x"
        assert self._skips(cmd, monkeypatch, "PRIVATE") is True

    def test_confirmed_public_api_write_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = "gh api repos/souliane/teatree/issues -f body=x"
        assert self._skips(cmd, monkeypatch, "PUBLIC") is False


class TestInertSubstitutionMarkerInBodyValue:
    """A substitution marker forces a SCAN only when bash would EXPAND it (#3357).

    ``_segment_carries_substitution_or_transport`` used to fire on the DECODED
    token value, so a ``$(``/backtick inside a SINGLE-quoted body value -- inert
    literal text bash passes verbatim (markdown inline code naming a flag/module
    is the everyday case) -- forced a SCAN and preempted the #3251 private-target
    skip, hard-blocking an ordinary private-target post. The fix reads the token's
    as-written source span so an INERT single-quoted marker no longer forces the
    scan, while the SECURITY invariants stand: a LIVE substitution (unquoted or
    double-quoted) still scans on EVERY target, and any marker toward a PUBLIC
    target still scans.
    """

    @staticmethod
    def _skips(command: str, monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> bool:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)
        return public_visibility.gate_skips_for_visibility(command, cwd=None)

    def test_single_quoted_backtick_in_body_toward_private_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inert markdown inline code in a private-target body value: the false trip.
        cmd = "gh issue create --repo owner/private-svc --body 'name the `flag` here'"
        assert self._skips(cmd, monkeypatch, "PRIVATE") is True

    def test_single_quoted_command_substitution_in_body_toward_private_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = "gh issue create --repo owner/private-svc --body 'run $(gh issue create) now'"
        assert self._skips(cmd, monkeypatch, "PRIVATE") is True

    def test_unquoted_live_substitution_toward_private_still_scans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A LIVE substitution can launch a hidden second command (a public post),
        # so it must NEVER skip -- not even toward a private primary target.
        cmd = "gh issue create --repo owner/private-svc --body $(cmd)"
        assert self._skips(cmd, monkeypatch, "PRIVATE") is False

    def test_double_quoted_live_substitution_toward_private_still_scans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = 'gh issue create --repo owner/private-svc --body "run $(cmd)"'
        assert self._skips(cmd, monkeypatch, "PRIVATE") is False

    def test_plain_private_body_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = "gh issue create --repo owner/private-svc --body plainbody"
        assert self._skips(cmd, monkeypatch, "PRIVATE") is True

    def test_inert_marker_toward_public_target_still_scans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Anti-vacuity: an inert marker does not weaken the gate on a PUBLIC target.
        cmd = "gh issue create --repo souliane/teatree --body 'name the `flag` here'"
        assert self._skips(cmd, monkeypatch, "PUBLIC") is False
