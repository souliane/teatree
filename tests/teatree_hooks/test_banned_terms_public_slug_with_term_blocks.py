"""A banned term on a slug-carries-term destination BLOCKS only when public.

The #2597 carve-out downgraded a banned-term block to a warn whenever the
resolved destination slug carried the term as a whole-token run -- with NO
visibility check. An org/repo slug is attacker-controllable (``<term>-eng/tracker``),
so a genuinely-public repo whose slug carries the term could silence the leak
block.

These tests pin the HARD invariant: a CONFIRMED-PUBLIC destination BLOCKS even
when the slug carries the term (the slug-text match must not vouch for a public
leak), AND a destination whose visibility the probe cannot confirm now FAILS
CLOSED and BLOCKS too (#3442 -- a probe error is not a skip; the attacker-
controllable ``<term>-eng/tracker`` slug can no longer silence the block by
merely being unresolvable in-hook). The companion
``TestProvablyPrivateDestinationStillAllowed`` proves a PROVABLY-private
destination (declared in the ``private_repos`` allowlist) is still skipped.

Synthetic terms only (``apple`` / ``democorp`` / ``othercorp``) -- the
overlay-leak-tree runs on PRs.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import _repo_visibility

# The two exploits the adversarial review reproduced: a genuinely-public repo
# (``apple/swift``) and an attacker-controllable org slug (``<term>-eng/...``),
# each carrying the banned term as a whole-token run of its own path.
_SLUG_CARRIES_TERM_EXPLOITS = [
    pytest.param(
        'gh issue create -R apple/swift --title x --body "apple is our customer, signed deal"',
        "apple",
        id="public-github-repo-named-after-term",
    ),
    pytest.param(
        'gh issue comment 5 -R democorp-eng/tracker --body "democorp customer config"',
        "democorp",
        id="attacker-controllable-org-slug",
    ),
]


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _pin_probe(monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> None:
    """Pin the live forge visibility probe (the only external subprocess).

    Mocking the probe keeps the test hermetic (no ``gh``/``glab``, no network)
    and deterministic: ``"PUBLIC"`` is a confirmed-public verdict, ``None`` is
    the indeterminate (tool-absent-in-hook) verdict the gate must fail closed on.
    """
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)


def _home_with_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    banned_terms: list[str] | None = None,
    private_repos: list[str] | None = None,
) -> Path:
    """Seed a DB-home config (legacy file tier removed) and isolate the probe cache."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    rows: dict[str, object] = {}
    if banned_terms is not None:
        rows["banned_terms"] = banned_terms
    if private_repos is not None:
        rows["private_repos"] = private_repos
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
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
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return home


@pytest.fixture
def banned_terms_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # No private_repos / internal_publish_namespaces: the destinations under
    # test are NOT provably-internal, so the deny path runs and the carve-out
    # (if any) decides the verdict.
    return _home_with_terms(tmp_path, monkeypatch, banned_terms=["apple", "democorp", "othercorp"])


class TestPublicSlugCarryingTermStillBlocks:
    @pytest.mark.parametrize(("command", "term"), _SLUG_CARRIES_TERM_EXPLOITS)
    def test_confirmed_public_destination_blocks(
        self,
        command: str,
        term: str,
        banned_terms_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The probe CONFIRMS the destination public; the slug carries the term.
        # The block must stand -- a public repo named after the term is not a
        # licence to leak the term onto it.
        _pin_probe(monkeypatch, "PUBLIC")
        blocked = handle_banned_terms_pretool(_bash(command))
        assert blocked is True, "a banned term on a CONFIRMED-PUBLIC slug-carries-term destination must BLOCK"
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert term in decision["permissionDecisionReason"]

    @pytest.mark.parametrize(("command", "term"), _SLUG_CARRIES_TERM_EXPLOITS)
    def test_probe_error_destination_fails_closed_and_blocks(
        self,
        command: str,
        term: str,
        banned_terms_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # #3442 fail closed: an indeterminate probe (tool absent / API error) on a
        # resolvable slug is NOT provably non-public, so the leak gate SCANS and the
        # slug-carried term BLOCKS -- the attacker-controllable slug can no longer
        # silence the block by being unresolvable in-hook. A genuinely-private repo
        # declares itself in ``private_repos`` (see the companion class).
        _pin_probe(monkeypatch, None)
        blocked = handle_banned_terms_pretool(_bash(command))
        decision = json.loads(capsys.readouterr().out)
        assert blocked is True, "a banned term on a probe-UNCONFIRMED destination must fail closed and BLOCK"
        assert decision["permissionDecision"] == "deny"
        assert term in decision["permissionDecisionReason"]


class TestProvablyPrivateDestinationStillAllowed:
    """The #2597 false positive is resolved the SOUND (config) way.

    A provably-private destination (declared in ``private_repos``) has the WHOLE
    banned-terms gate skipped by ``gate_skips_for_visibility`` -- the overlay name
    on its own private surface is not a leak. This proves the gate blocks only on
    an affirmatively-public destination.
    """

    def test_private_tracker_in_allowlist_is_allowed_offline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The offline allowlist alone proves the destination internal -- no live
        # probe (pinned indeterminate) is needed for the #2597 resolution path.
        _home_with_terms(
            tmp_path,
            monkeypatch,
            private_repos=["democorp-eng/tracker"],
            banned_terms=["democorp"],
        )
        _pin_probe(monkeypatch, None)
        cmd = 'gh issue comment 5 -R democorp-eng/tracker --body "democorp customer config"'
        assert handle_banned_terms_pretool(_bash(cmd)) is False, (
            "a post to the overlay's OWN provably-private tracker (in private_repos) "
            "must be ALLOWED via the destination gate skip (#2597 resolution)"
        )
