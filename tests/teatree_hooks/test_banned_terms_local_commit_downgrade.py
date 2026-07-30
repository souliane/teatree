"""A readable-body banned term on a LOCAL git commit downgrades to warn (#1415, Case A).

The UNREADABLE-body commit path already downgraded ANY local commit to warn
(``marker.py`` → ``command_targets_local_commit``): a commit is LOCAL, and the
#703 pre-push gate re-scans commit messages for banned terms before they reach a
public remote. The READABLE-body path (``deny.py`` → ``carve_out_applies`` →
``commit_branch_downgrades``) required a PROVABLY-private landing repo, so a plain
``git commit -m "...<term>..."`` in the user's own repo that is neither in
``private_repos`` nor probe-resolvable HARD-BLOCKED — forcing the clumsy
``ALLOW_BANNED_TERM=1`` override. This asymmetry is the bug.

These tests pin the fix and its leak-safety:

- GREEN (Case A): a plain readable-body commit in an unknown-visibility repo
    DOWNGRADES to a stderr warn (no deny), parametrized over the bare org slug /
    the compound overlay term and over ``-m`` / ``-F`` body-file.
- RED anti-vacuity: a commit CHAINED to a public ``gh`` post, and a pure public
    ``gh`` post, both still HARD-BLOCK — the widening never relaxes a real public
    surface. The chained-segment proof defeats the commit downgrade.
- The whole-token matcher is pinned so the term the commit trips on is a genuine
    whole-token match, never an over-eager substring of a longer host token.

Synthetic terms only (``democorp`` / ``democorp-factory``) — the public repo
never carries a real configured term.
"""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import _repo_visibility, banned_terms_scanner

_PUBLIC_SLUG = "souliane/teatree"


def _seed_config_db(db_path: Path, rows: dict[str, object]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
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


def _home_with_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    banned_terms: list[str],
    private_repos: list[str] | None = None,
) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    rows: dict[str, object] = {"banned_terms": banned_terms}
    if private_repos is not None:
        rows["private_repos"] = private_repos
    db = home / "config.sqlite3"
    _seed_config_db(db, rows)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GH_REPO", raising=False)
    return home


def _git(cwd: Path, *args: str) -> None:
    git_bin = shutil.which("git")
    assert git_bin is not None
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run([git_bin, *args], cwd=cwd, check=True, capture_output=True, env=env)


def _repo(tmp_path: Path, name: str, origin: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "remote", "add", "origin", origin)
    return repo


def _unknown_visibility_repo(tmp_path: Path, name: str = "own-repo") -> Path:
    # A user-owned checkout NOT declared in private_repos: with the probe tool
    # unresolvable in-hook, its visibility is UNKNOWN — the exact Case A shape.
    return _repo(tmp_path, name, "git@github.com:someorg/some-product.git")


def _pin_probe_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    # No probe tool reachable in-hook → every slug resolves to UNKNOWN visibility.
    monkeypatch.setattr(_repo_visibility, "_resolve_probe_tool", lambda _tool: None)


def _pin_probe_public_only(monkeypatch: pytest.MonkeyPatch, public_slug: str) -> None:
    # Only ``public_slug`` is affirmatively public; every other slug is UNKNOWN.
    monkeypatch.setattr(
        _repo_visibility,
        "probe_visibility",
        lambda slug: "PUBLIC" if public_slug in slug else None,
    )


def _bash(command: str, cwd: Path) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


# ── GREEN (Case A): a readable-body commit downgrades to warn ─────────────────
#
# (banned_terms, term) parametrized over the bare org slug and the compound
# overlay term; each spelled so exactly one configured term matches (the
# reported term is deterministic for the warning assertion).
_CASE_A_TERMS = [
    pytest.param(["democorp"], "democorp", id="bare-org-slug"),
    pytest.param(["democorp-factory"], "democorp-factory", id="compound-overlay-term"),
]


class TestReadableCommitDowngradesToWarn:
    @pytest.mark.parametrize(("banned_terms", "term"), _CASE_A_TERMS)
    def test_dash_m_commit_in_unknown_visibility_repo_warns(
        self,
        banned_terms: list[str],
        term: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _home_with_config(tmp_path, monkeypatch, banned_terms=banned_terms)
        _pin_probe_unresolvable(monkeypatch)
        repo = _unknown_visibility_repo(tmp_path)

        command = f'git commit -m "feat: onboard {term} pipeline"'
        blocked = handle_banned_terms_pretool(_bash(command, repo))
        captured = capsys.readouterr()

        assert blocked is False, "a plain readable-body commit in an own unknown-visibility repo must WARN, not block"
        assert captured.out == ""  # no deny JSON
        assert "downgraded to warn" in captured.err
        assert term in captured.err

    @pytest.mark.parametrize(("banned_terms", "term"), _CASE_A_TERMS)
    def test_dash_f_body_file_commit_in_unknown_visibility_repo_warns(
        self,
        banned_terms: list[str],
        term: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _home_with_config(tmp_path, monkeypatch, banned_terms=banned_terms)
        _pin_probe_unresolvable(monkeypatch)
        repo = _unknown_visibility_repo(tmp_path)

        body = repo / "COMMIT_MSG.txt"
        body.write_text(f"feat: build the {term} rollout\n", encoding="utf-8")
        command = f"git add -A && git commit -F {body}"
        blocked = handle_banned_terms_pretool(_bash(command, repo))
        captured = capsys.readouterr()

        assert blocked is False, "a readable -F body-file commit in an own unknown-visibility repo must WARN"
        assert captured.out == ""  # no deny JSON
        assert "downgraded to warn" in captured.err
        assert term in captured.err


# ── RED anti-vacuity: real public surfaces still hard-block ───────────────────
#
# The commit-body shape is parametrized over -m and a readable -F body-file so
# neither spelling of the tripped commit can silence the chained public post.
_CHAINED_COMMIT_SHAPES = [
    pytest.param('git commit -m "feat: onboard democorp"', id="dash-m"),
    pytest.param("git add -A && git commit -F {body}", id="dash-f-body-file"),
]


class TestPublicSurfaceStillBlocks:
    @pytest.mark.parametrize("commit_shape", _CHAINED_COMMIT_SHAPES)
    def test_commit_chained_to_public_gh_post_still_blocks(
        self,
        commit_shape: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # RED-a: the commit carries the term, but a chained PUBLIC ``gh issue
        # create`` in the same command means the local-commit downgrade must NOT
        # apply — the chained-segment proof defeats it and the post hard-blocks.
        _home_with_config(tmp_path, monkeypatch, banned_terms=["democorp"])
        _pin_probe_public_only(monkeypatch, _PUBLIC_SLUG)
        repo = _unknown_visibility_repo(tmp_path)

        body = repo / "COMMIT_MSG.txt"
        body.write_text("feat: onboard democorp\n", encoding="utf-8")
        commit = commit_shape.format(body=body)
        command = f'{commit} && gh issue create --repo {_PUBLIC_SLUG} --title x --body "clean public body"'

        blocked = handle_banned_terms_pretool(_bash(command, repo))
        captured = capsys.readouterr()

        assert blocked is True, "a local commit chained to a PUBLIC gh post must stay hard-blocked"
        decision = json.loads(captured.out)
        assert decision["permissionDecision"] == "deny"
        assert "democorp" in decision["permissionDecisionReason"]

    def test_pure_public_gh_post_still_blocks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # RED-b: a pure public ``gh issue create`` carrying the term is unaffected
        # by the commit-only downgrade — it has no commit segment and still blocks.
        _home_with_config(tmp_path, monkeypatch, banned_terms=["democorp"])
        _pin_probe_public_only(monkeypatch, "apple/swift")
        repo = _unknown_visibility_repo(tmp_path)

        command = 'gh issue create -R apple/swift --title x --body "rolling out democorp integration"'
        blocked = handle_banned_terms_pretool(_bash(command, repo))
        captured = capsys.readouterr()

        assert blocked is True, "a pure public gh post carrying the term must stay hard-blocked"
        decision = json.loads(captured.out)
        assert decision["permissionDecision"] == "deny"
        assert "democorp" in decision["permissionDecisionReason"]


# ── GREEN-b: whole-token matcher pin (substring in a host token does NOT match) ─
#
# The downgrade is only leak-safe if the term the commit trips on is a genuine
# whole-token match. A bare org slug that is a pure SUBSTRING of a longer URL
# host token (``democorp`` inside ``democorporation``) must NOT match, so the
# gate never fires — and never downgrades — on a coincidental substring.


class TestWholeTokenMatcherPin:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("feat: see https://democorporation.example.com/x", None, id="substring-in-host-token"),
            pytest.param("feat: onboard democorp customer", "democorp", id="whole-token"),
        ],
    )
    def test_org_term_matches_whole_token_only(
        self,
        text: str,
        expected: str | None,
        tmp_path: Path,
    ) -> None:
        cfg = tmp_path / "cfg.sqlite3"
        _seed_config_db(cfg, {"banned_terms": ["democorp"]})
        assert banned_terms_scanner.scan_text(text, config_path=cfg) == expected
