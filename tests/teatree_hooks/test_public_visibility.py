"""The pre-publish leak gates skip ONLY a PROVABLY non-public target (#1415/#1213, #3442).

The banned-terms (#1415) and quote-scanner (#1213) PUBLISH gates protect against
leaking internal vocabulary / user quotes onto PUBLIC surfaces. A leak scan is
SKIPPED only when the target repository is PROVABLY non-public: an
allowlisted-private / internal-namespace slug, or a probe-CONFIRMED
private/internal repo. A target the gate cannot prove non-public -- an
affirmatively-``public`` probe verdict, OR a RESOLVABLE ``owner/repo`` slug whose
visibility probe could not be confirmed (a network/API error, an absent
``gh``/``glab``, an unrecognised answer) -- is SCANNED. The probe-error case FAILS
CLOSED (#3442): it agrees with the bash pre-push mirror
(:file:`scripts/hooks/refuse-public-push-with-leak.sh`) and the fail-closed-always
leak-gate doctrine, and it is never a silent skip.

These tests drive BOTH live handlers across the visibility matrix with the forge
visibility probe mocked (no ``gh``/``glab``, no network): a PUBLIC target carrying
a leak FIRES, a probe-CONFIRMED-PRIVATE target carrying the SAME leak SKIPS, and a
RESOLVABLE target whose probe cannot confirm visibility FIRES (fail closed). The
private SKIP row is the anti-vacuity guard -- it proves the gate is green there
because the target is provably non-public, not because it stopped detecting the
leak. The unit block pins the polarity of the
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
def test_probe_error_resolvable_target_with_leak_fires(
    handler: Handler,
    leak_body: str,
    reason_token: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #3442 fail-closed: the SAME leak toward a RESOLVABLE ``owner/repo`` slug the
    # in-hook probe cannot confirm (returns None -- a network/API error, absent
    # tool, or unrecognised answer) is NOT provably non-public, so the gate FAILS
    # CLOSED and SCANS -- the leak is denied, mirroring the bash pre-push gate.
    # (Previously this skipped, letting a probe error ride the leak out unscanned.)
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
    blocked = handler(_post("someowner/mystery", leak_body))
    decision = json.loads(capsys.readouterr().out)
    assert blocked is True
    assert decision["permissionDecision"] == "deny"
    assert reason_token in decision["permissionDecisionReason"]


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

    def test_probe_error_resolvable_target_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # #3442: a resolvable slug whose probe cannot confirm visibility (None)
        # FAILS CLOSED -- the gate scans, it does not skip.
        assert self._skips("gh issue create --repo owner/mystery --body x", monkeypatch, None) is False

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


class TestUnresolvedStructuredWriteDoesNotSkip:
    """#F7.2: a ``gh``/``glab`` structured WRITE whose destination did NOT resolve SCANS.

    An unresolved ``gh``/``glab`` publish is NOT provably non-public, so it must
    FAIL CLOSED (scan), never skip. The old fallback returned ``_SKIP_PUBLISH`` for
    any ``gh``/``glab``-led segment even when the destination was ``None`` -- so a
    ``gh pr review 5 --body <HIGH quote>`` (a verb not resolved to the current
    repo) skipped the leak scan entirely. Only a RESOLVED provably-non-public
    destination earns a skip.
    """

    @staticmethod
    def _skips(command: str, monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> bool:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)
        # No cwd -> a flagless verb cannot resolve a current-repo destination.
        return public_visibility.gate_skips_for_visibility(command, cwd=None)

    def test_flagless_pr_review_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = 'gh pr review 5 --body "please ship now"'
        assert self._skips(cmd, monkeypatch, None) is False

    def test_flagless_pr_create_with_no_cwd_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = 'gh pr create --title t --body "please ship now"'
        assert self._skips(cmd, monkeypatch, None) is False

    def test_resolved_private_flag_target_still_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Over-block guard: a RESOLVED, probe-confirmed-private ``--repo`` target
        # still skips -- the fail-closed change only affects UNRESOLVED dests.
        cmd = 'gh pr review 5 --repo owner/private-svc --body "note"'
        assert self._skips(cmd, monkeypatch, "PRIVATE") is True


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


class TestProbeErrorFailsClosed:
    """A visibility-probe error on a RESOLVABLE slug FAILS CLOSED, never silently skips (#3442).

    The leak gate is fail-closed-always doctrine: it must SCAN every target it
    cannot PROVE non-public. Before #3442 a probe error (``slug_visibility ->
    None``) on a resolvable ``owner/repo`` slug made the gate SKIP -- the same
    fail-OPEN the bash pre-push mirror was hardened against (§3f #14). These rows
    pin the reconciled polarity: the structured post AND the raw ``api`` WRITE both
    scan on a probe error, the decision is signalled on stderr (never silent), and
    a PROVABLY-private target (allowlist / confirmed-private probe) still skips so
    the fix does not over-block a declared-private repo.
    """

    @staticmethod
    def _skips(command: str, monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> bool:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)
        return public_visibility.gate_skips_for_visibility(command, cwd=None)

    def test_structured_post_probe_error_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # RED before #3442: probe None on a resolvable, non-allowlisted slug SKIPPED.
        cmd = "gh issue create --repo someowner/mystery --body leak"
        assert self._skips(cmd, monkeypatch, None) is False

    def test_api_write_probe_error_does_not_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A raw ``gh api`` WRITE whose URL resolves a slug the probe cannot confirm
        # is an immediate public egress with no pre-push backstop -- fail closed.
        cmd = "gh api repos/someowner/mystery/issues -f body=leak"
        assert self._skips(cmd, monkeypatch, None) is False

    def test_probe_error_scan_is_signalled_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ALWAYS log a probe-error-driven decision so it is never silent (#3442),
        # mirroring the bash gate's ``echo ... >&2`` on undetermined visibility.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        public_visibility.gate_skips_for_visibility("gh issue create --repo someowner/mystery --body x", cwd=None)
        err = capsys.readouterr().err
        assert "someowner/mystery" in err
        assert "fail closed" in err

    def test_confirmed_private_probe_still_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Anti-over-block: a probe-CONFIRMED-private target is provably non-public,
        # so it still skips -- the fix narrows the scan to the UNCONFIRMED case.
        cmd = "gh issue create --repo someowner/private-svc --body x"
        assert self._skips(cmd, monkeypatch, "PRIVATE") is True

    def test_allowlisted_private_skips_without_probe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The offline allowlist is the network-free way to keep an own-private post
        # skip-eligible even when the probe cannot run -- so the fail-closed change
        # does not over-block a DECLARED-private repo.
        db = tmp_path / "config.sqlite3"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS teatree_config_setting "
                "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'private_repos', ?)",
                (json.dumps(["declaredowner/svc"]),),
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        cmd = "gh issue create --repo declaredowner/svc --body x"
        assert public_visibility.gate_skips_for_visibility(cmd, cwd=None, config_path=db) is True
