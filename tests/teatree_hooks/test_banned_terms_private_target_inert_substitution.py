"""A private-target post is not blocked by an INERT (single-quoted) body marker (#1415).

The banned-terms posting gate skips a post whose target repo is provably
non-public (``private_repos`` allowlist or a PRIVATE probe verdict) -- an
overlay/customer term on the overlay's OWN private repo is not a public leak.
That skip is decided by :func:`public_visibility.gate_skips_for_visibility`,
which forced a full SCAN (and hence a hard block) whenever ANY token of the
command carried a command/process-substitution marker (``$(``, ``<(``, ``>(``,
backtick) -- so it could never hide a chained public post inside a live
``$(...)``. The marker check read the DECODED token value, so it could not tell
a LIVE substitution from one bash keeps as inert literal text inside SINGLE
quotes. A perfectly ordinary MR/PR description to a PRIVATE repo -- markdown
inline code in single quotes (``--description 'refactor the `svc` module'``) --
therefore hard-blocked with a banned-term deny, even though the backtick is
inert and the body never leaves the private repo.

These tests pin the fix and its security invariants:

- an INERT single-quoted substitution marker in a PRIVATE-target body is ALLOWED
    (the #3357 false positive);
- a LIVE substitution that does NOT detectably publish (``$(cat body.md)``,
    ``$(echo …)``) toward a LITERAL provably-private target is ALLOWED too --
    including the #2369 scan-impossibility shapes whose body cannot be read at
    scan time (a private target has no public-leak surface, so no escape flag is
    ever needed there, #1213/#1415);
- a live substitution that DETECTABLY publishes (``$(gh … create …)``) still
    forces the SCAN and BLOCKS even on a private target -- the hidden-public-post
    vector stays closed;
- the SAME shapes toward a PUBLIC or UNKNOWN-visibility target still BLOCK (the
    private skip must not weaken the fail-closed public-surface scan);
- a high-confidence SECRET in a private-target body still BLOCKS (secrets leak on
    every surface, scanned before any visibility skip); and
- the ``ALLOW_BANNED_TERM=1`` escape is unchanged.

Synthetic terms only (``democorp``) -- the overlay-leak-tree runs on real PRs.
"""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import _repo_visibility, public_visibility

_TERM = "democorp"
_PRIVATE_NS = "democorp-engineering"


def _seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    private_repos: list[str] | None = None,
) -> None:
    """Seed a DB-home config with the banned term (and optional private_repos) and isolate state."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    rows: dict[str, object] = {"banned_terms": [_TERM]}
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True)  # noqa: S607 — real git under tmp_path (repo test doctrine)


def _repo(tmp_path: Path, remote: str) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", remote)
    return repo


def _bash(command: str, cwd: Path | None = None) -> dict[str, object]:
    data: dict[str, object] = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        data["cwd"] = str(cwd)
    return data


def _pin_probe(monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> None:
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)


class TestInertSubstitutionOnPrivateTarget:
    """A single-quoted (inert) substitution marker in a private-target body is ALLOWED."""

    def test_backtick_inline_code_in_private_body_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The offline allowlist alone proves the target private; no probe needed.
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = f"glab mr create -R {_PRIVATE_NS}/skills --title t --description 'refactor the `{_TERM}` module'"
        assert handle_banned_terms_pretool(_bash(cmd)) is False, (
            "a single-quoted (inert) backtick in a private-target body must NOT hard-block"
        )
        assert capsys.readouterr().out == ""  # no deny JSON

    def test_command_substitution_literal_in_private_body_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = f"glab mr create -R {_PRIVATE_NS}/skills --title t --description 'see $({_TERM}) note'"
        assert handle_banned_terms_pretool(_bash(cmd)) is False, (
            "a single-quoted (inert) $(...) literal in a private-target body must NOT hard-block"
        )
        assert capsys.readouterr().out == ""

    def test_inert_marker_private_target_via_cwd_remote_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No -R flag: the private target resolves from the cwd git remote.
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        repo = _repo(tmp_path, f"git@gitlab.com:{_PRIVATE_NS}/skills.git")
        cmd = f"glab mr create --source-branch x --target-branch main --title t --description 'the `{_TERM}` bit'"
        assert handle_banned_terms_pretool(_bash(cmd, cwd=repo)) is False
        assert capsys.readouterr().out == ""

    def test_plain_body_private_target_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Anti-regression lock on the plain (no-marker) private-target case.
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = f"glab mr create -R {_PRIVATE_NS}/skills --title t --description 'rolling out {_TERM} support'"
        assert handle_banned_terms_pretool(_bash(cmd)) is False
        assert capsys.readouterr().out == ""


class TestLiveNonPublishingSubstitutionOnPrivateTarget:
    """A live substitution that does NOT detectably publish never blocks a private post.

    The #1213/#1415 friction: every multi-line-body ``glab mr create`` shape
    (``-d "$(cat body.md)"``, a ``VAR``/stdin body -- glab has no
    ``--description-file`` flag) either tripped the substitution pre-check or
    produced an unresolvable-body sentinel, hard-blocking a provably-PRIVATE MR
    and forcing the ALLOW_BANNED_TERM/QUOTE_OK escapes on essentially every MR.
    A provably-private target has no public-leak surface, so these shapes now
    SKIP; only a substitution that DETECTABLY publishes (a ``$(gh … create …)``)
    keeps the scan (see TestSecurityInvariantsUnchanged).
    """

    def test_live_double_quoted_substitution_on_private_target_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = f'glab mr create -R {_PRIVATE_NS}/skills --title t --description "x $(echo {_TERM})"'
        assert handle_banned_terms_pretool(_bash(cmd)) is False, (
            "a non-publishing live substitution toward a provably-private target must not block"
        )
        assert capsys.readouterr().out == ""

    def test_live_unquoted_substitution_on_private_target_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = f"glab mr create -R {_PRIVATE_NS}/skills --title t --description=$(echo {_TERM})"
        assert handle_banned_terms_pretool(_bash(cmd)) is False
        assert capsys.readouterr().out == ""

    def test_unreadable_cat_body_on_private_target_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The #2369 scan-impossibility shape: the body file cannot be read at
        # scan time, but the target is provably private -- nothing to leak, so
        # the fail-closed sentinel must not block (no escape flag needed).
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = f'glab mr create -R {_PRIVATE_NS}/skills --title t --description "$(cat {tmp_path}/absent-body.md)"'
        assert handle_banned_terms_pretool(_bash(cmd)) is False
        assert capsys.readouterr().out == ""


class TestSecurityInvariantsUnchanged:
    """The hidden-publish, public-target, unknown-target and secret blocks all stand."""

    def test_substitution_hiding_forge_post_on_private_target_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The load-bearing residual: a live $(...) that ITSELF carries a
        # detectable publish invocation could post to a PUBLIC repo when the
        # shell expands it, so it must never ride the private-target skip.
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = (
            f"glab mr create -R {_PRIVATE_NS}/skills --title t "
            f'--description "$(gh issue create -R someowner/public-svc -b {_TERM})"'
        )
        assert handle_banned_terms_pretool(_bash(cmd)) is True, (
            "a substitution hiding a forge publish must still hard-block even on a private target"
        )
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_process_substitution_hiding_forge_post_is_not_given_a_free_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ``<(...)`` twin of the hidden-publish block: a process substitution
        # spawning a forge publish must keep the visibility skip from firing.
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        cmd = (
            f"glab mr create -R {_PRIVATE_NS}/skills --title t "
            f"--description <(gh issue create -R someowner/public-svc -b {_TERM})"
        )
        assert public_visibility.gate_skips_for_visibility(cmd, cwd=None) is False

    def test_unreadable_body_on_unknown_target_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # UNKNOWN visibility (probe unavailable, not allowlisted) keeps the
        # fail-closed scan-impossibility block -- only a PROVABLY-private target
        # earns the skip (#3442).
        _seed(tmp_path, monkeypatch)  # no private_repos
        _pin_probe(monkeypatch, None)
        cmd = f'glab mr create -R someowner/mystery-svc --title t --description "$(cat {tmp_path}/absent-body.md)"'
        assert handle_banned_terms_pretool(_bash(cmd)) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_unreadable_body_on_public_target_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A confirmed-PUBLIC target with a body the gate cannot read stays
        # fail-closed -- the private skip must not weaken the public scan.
        _seed(tmp_path, monkeypatch)  # no private_repos
        _pin_probe(monkeypatch, "PUBLIC")
        cmd = f'gh pr create --repo someowner/public-svc --title t --body "$(cat {tmp_path}/absent-body.md)"'
        assert handle_banned_terms_pretool(_bash(cmd)) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_inert_marker_on_public_target_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The SAME inert single-quoted body toward a CONFIRMED-PUBLIC target is a
        # real public leak -- quote-awareness must not weaken the public scan.
        _seed(tmp_path, monkeypatch)  # no private_repos
        _pin_probe(monkeypatch, "PUBLIC")
        cmd = f"gh pr create --repo someowner/public-svc --title t --body 'refactor the `{_TERM}` module'"
        assert handle_banned_terms_pretool(_bash(cmd)) is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert _TERM in decision["permissionDecisionReason"]

    def test_secret_in_private_target_body_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Secrets leak on EVERY surface, including a private repo, and are scanned
        # before any visibility skip -- so a token in a private-target body blocks.
        _seed(tmp_path, monkeypatch, private_repos=[_PRIVATE_NS])
        _pin_probe(monkeypatch, None)
        token = "ghp_" + "A" * 36  # matches the GitHub PAT secret shape
        cmd = f"glab mr create -R {_PRIVATE_NS}/skills --title t --description 'token {token}'"
        assert handle_banned_terms_pretool(_bash(cmd)) is True, "a secret must block even on a private target"
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_allow_banned_term_override_still_bypasses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The explicit override is unchanged: a public target with a banned term is
        # allowed through only when the leading env prefix opts out.
        _seed(tmp_path, monkeypatch)  # no private_repos
        _pin_probe(monkeypatch, "PUBLIC")
        cmd = f"ALLOW_BANNED_TERM=1 gh pr create --repo someowner/public-svc --title t --body 'ship {_TERM} now'"
        assert handle_banned_terms_pretool(_bash(cmd)) is False
        assert capsys.readouterr().out == ""
