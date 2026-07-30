"""The commit gate scans the staged DIFF's added lines, not the whole file.

The ``banned-terms`` pre-commit hook used to scan every staged FILE in full
(:func:`teatree.hooks.term_match.file_matches`), so staging a one-line change
to a file that ALREADY carried a committed banned term blocked the commit on
the pre-existing line the diff never touched — the recurring #1415 over-block
(a ``BLUEPRINT.md`` edit blocked because a far-away committed line names a
private term).

The ``--diff-only`` mode scopes the commit hook to the staged diff's ADDED
(``+``) lines per file. A pre-existing committed banned-term line no longer
blocks an unrelated commit, while a NEWLY-ADDED banned-term line still blocks.
The default (no flag) keeps the full-file scan the posting gate and the parity
meta-test depend on — only the pre-commit hook entry passes ``--diff-only``.

The term list is DB-home: tests seed a ``teatree_config_setting`` sqlite DB and
point the reader at it via ``T3_CONFIG_DB`` (subprocess) or ``db_path``
(in-process). All terms here are SYNTHETIC (``acme`` stands in for a real
single-token customer term); no real configured value appears in this public
source.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.hooks import banned_terms_cli
from teatree.hooks.banned_terms_cli import (
    _diff_only_report,
    _full_file_report,
    _load_allowlist,
    main,
    resolve_banned_terms,
    staged_added_lines,
)
from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError

_TERMS = ("acme",)


@pytest.fixture(autouse=True)
def _no_ambient_terms_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any ambient ``T3_BANNED_TERMS`` so the DB is the only source under test."""
    monkeypatch.delenv("T3_BANNED_TERMS", raising=False)


def _seed_db(tmp_path: Path, terms: list[str], *, allowlist: list[str] | None = None) -> Path:
    """Build a ``teatree_config_setting`` DB carrying the banned-terms rows."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
            (json.dumps(terms),),
        )
        if allowlist is not None:
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms_allowlist', ?)",
                (json.dumps(allowlist),),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _run_cli(repo: Path, db: Path, *files: str, diff_only: bool = False) -> subprocess.CompletedProcess[str]:
    """Invoke the banned_terms_cli module exactly as the pre-commit hook does.

    Runs with ``cwd=repo`` (prek runs hooks from the repo root and passes
    repo-relative paths) so the staged diff resolves against the right repo, and
    points the DB-home reader at the seeded DB via ``T3_CONFIG_DB``.
    """
    argv = ["--diff-only"] if diff_only else []
    argv.extend(files)
    return subprocess.run(
        [sys.executable, "-m", "teatree.hooks.banned_terms_cli", *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "T3_CONFIG_DB": str(db)},
    )


def _init_repo_with_committed_banned_line(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose committed ``doc.md`` carries a pre-existing banned-term line.

    Returns ``(repo, db)``. Line 1 holds the banned term ``acme``; line 2
    is a clean line. The banned line is already committed, so it is NOT in any
    later staged diff.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    doc = repo / "doc.md"
    doc.write_text("acme reference on a pre-existing committed line\nclean line two\n", encoding="utf-8")
    _git(repo, "add", "doc.md")
    _git(repo, "commit", "-m", "seed: committed doc with a pre-existing term line")
    db = _seed_db(tmp_path, list(_TERMS))
    return repo, db


# ── (a) pre-existing committed banned line + staged change to a DIFFERENT line ──
# RED before the fix: full-file scan flags the committed line-1 term and blocks.
# GREEN after: diff-only scans only the staged added line, which is clean.


def test_diff_only_allows_commit_when_banned_term_only_on_preexisting_line(tmp_path: Path) -> None:
    repo, db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    # Stage a change to line 2 only — line 1 (the banned term) is untouched.
    doc.write_text("acme reference on a pre-existing committed line\nedited clean line two\n", encoding="utf-8")
    _git(repo, "add", "doc.md")

    result = _run_cli(repo, db, "doc.md", diff_only=True)

    assert result.returncode == 0, f"diff-only must ALLOW an unrelated edit; got:\n{result.stdout}\n{result.stderr}"


def test_full_file_scan_still_blocks_the_same_preexisting_line(tmp_path: Path) -> None:
    # ANTI-VACUITY for (a): WITHOUT --diff-only, the SAME staged tree still
    # blocks on the committed line-1 term. Proves the allow above measures the
    # diff-only narrowing, not a CLI that stopped flagging this file at all.
    repo, db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    doc.write_text("acme reference on a pre-existing committed line\nedited clean line two\n", encoding="utf-8")
    _git(repo, "add", "doc.md")

    result = _run_cli(repo, db, "doc.md", diff_only=False)

    assert result.returncode == 1
    assert "BANNED TERM in doc.md" in result.stdout


# ── (b) staged change that ADDS a new banned-term line → still BLOCKED ──
# Must stay green under --diff-only: the gate is narrowed to the diff, NOT gutted.


def test_diff_only_blocks_commit_that_adds_a_new_banned_term_line(tmp_path: Path) -> None:
    repo, db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    # Add a NEW line carrying the banned term (line 1 stays untouched).
    doc.write_text(
        "acme reference on a pre-existing committed line\nclean line two\nnewly added acme leak line\n",
        encoding="utf-8",
    )
    _git(repo, "add", "doc.md")

    result = _run_cli(repo, db, "doc.md", diff_only=True)

    assert result.returncode == 1, "a newly-added banned-term line must STILL block"
    assert "BANNED TERM in doc.md" in result.stdout
    assert "newly added acme leak line" in result.stdout


def test_diff_only_blocks_brand_new_file_with_banned_term(tmp_path: Path) -> None:
    # A brand-new staged file is all added lines, so diff-only blocks it the
    # same as a full-file scan would — no committed baseline to exempt.
    repo, db = _init_repo_with_committed_banned_line(tmp_path)
    new_file = repo / "new.md"
    new_file.write_text("a fresh file introducing acme\n", encoding="utf-8")
    _git(repo, "add", "new.md")

    result = _run_cli(repo, db, "new.md", diff_only=True)

    assert result.returncode == 1
    assert "BANNED TERM in new.md" in result.stdout


def test_diff_only_allows_when_only_clean_lines_are_added(tmp_path: Path) -> None:
    repo, db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    doc.write_text(
        "acme reference on a pre-existing committed line\nclean line two\na perfectly clean new line\n",
        encoding="utf-8",
    )
    _git(repo, "add", "doc.md")

    result = _run_cli(repo, db, "doc.md", diff_only=True)

    assert result.returncode == 0, result.stdout


def test_diff_only_falls_back_to_full_scan_outside_a_git_repo(tmp_path: Path) -> None:
    # FAIL-CLOSED: if the staged diff cannot be resolved (no git repo / git
    # error), diff-only must NOT fail open. It falls back to the full-file scan
    # so a banned term in the file is still caught.
    db = _seed_db(tmp_path, list(_TERMS))
    loose = tmp_path / "loose.md"
    loose.write_text("acme in a file with no git repo around it\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "teatree.hooks.banned_terms_cli", "--diff-only", "loose.md"],
        cwd=tmp_path,  # not a git repo
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "T3_CONFIG_DB": str(db)},
    )

    assert result.returncode == 1, "outside a git repo, diff-only must fall back to a full scan, not fail open"
    assert "BANNED TERM in loose.md" in result.stdout


# ── (c) diff-evasion: a banned term on a content line that begins with ``+`` ──
# In ``git diff --cached -U0`` output an ADDED content line is rendered as the
# add-marker ``+`` followed by the line's own text. So a staged content line
# ``++acme`` appears as ``+++acme``, ``+++acme`` as ``++++acme``, and ``+++ acme``
# as ``++++ acme``. A naive ``not line.startswith("+++")`` filter (meant only to
# drop the unified-diff ``+++ <file>`` header) ALSO drops these real content
# lines, so a banned term staged on such a line slips the commit gate (fail-open
# diff-evasion). The hunk-aware parse keeps them — they live inside a hunk body,
# whereas the ``+++ b/<file>`` header is pre-hunk and never collected.


@pytest.mark.parametrize(
    "content_line",
    [
        "++acme glued evasion line",  # rendered as +++acme... in the diff
        "+++acme triple evasion line",  # rendered as ++++acme...
        "+++ acme spaced evasion line",  # rendered as ++++ acme... — defeats a "+++ " match too
    ],
)
def test_diff_only_blocks_added_content_line_starting_with_plus(tmp_path: Path, content_line: str) -> None:
    repo, db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    # Add a NEW content line whose own text begins with ``+`` and carries the
    # banned term. The committed line 1 stays untouched.
    doc.write_text(
        f"acme reference on a pre-existing committed line\nclean line two\n{content_line}\n",
        encoding="utf-8",
    )
    _git(repo, "add", "doc.md")

    result = _run_cli(repo, db, "doc.md", diff_only=True)

    assert result.returncode == 1, (
        f"a banned term on an added content line beginning with '+' must STILL block "
        f"(diff-evasion); got:\n{result.stdout}\n{result.stderr}"
    )
    assert "BANNED TERM in doc.md" in result.stdout


@pytest.mark.parametrize(
    "content_line",
    [
        "++acme glued evasion line",
        "+++acme triple evasion line",
        "+++ acme spaced evasion line",
    ],
)
def test_staged_added_lines_keeps_content_line_starting_with_plus(tmp_path: Path, content_line: str) -> None:
    # Helper-level proof: the extractor returns the full content line verbatim
    # (leading ``+`` chars preserved), never confusing it with the ``+++`` file
    # header. This is the unit guard for the diff-evasion regression above.
    repo, _db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    doc.write_text(
        f"acme reference on a pre-existing committed line\nclean line two\n{content_line}\n",
        encoding="utf-8",
    )
    _git(repo, "add", "doc.md")

    added = staged_added_lines(repo, "doc.md")

    assert added == [content_line]


# ── helper-level unit tests: staged added-line extraction ──


def test_staged_added_lines_returns_only_added_lines(tmp_path: Path) -> None:
    repo, _db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    doc.write_text(
        "acme reference on a pre-existing committed line\nclean line two\nbrand new line added\n",
        encoding="utf-8",
    )
    _git(repo, "add", "doc.md")

    added = staged_added_lines(repo, "doc.md")

    assert added == ["brand new line added"]


def test_staged_added_lines_empty_when_no_staged_change(tmp_path: Path) -> None:
    repo, _db = _init_repo_with_committed_banned_line(tmp_path)

    # Nothing staged for doc.md → no added lines.
    assert staged_added_lines(repo, "doc.md") == []


def test_staged_added_lines_returns_none_outside_git_repo(tmp_path: Path) -> None:
    # A None return is the sentinel "could not resolve the staged diff" — the
    # caller falls back to a full-file scan (fail closed), never fail open.
    assert staged_added_lines(tmp_path, "doc.md") is None


# ── helper-level unit tests: in-process report builders ──


def test_diff_only_report_flags_only_added_banned_line(tmp_path: Path) -> None:
    repo, _db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    doc.write_text(
        "acme reference on a pre-existing committed line\nclean line two\nadded acme line\n",
        encoding="utf-8",
    )
    _git(repo, "add", "doc.md")

    report = _diff_only_report(["doc.md"], _TERMS, repo)

    assert report == ["BANNED TERM in doc.md:", "  +:added acme line"]


def test_diff_only_report_clean_added_line_is_empty(tmp_path: Path) -> None:
    repo, _db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"
    doc.write_text(
        "acme reference on a pre-existing committed line\nclean line two\nperfectly clean addition\n",
        encoding="utf-8",
    )
    _git(repo, "add", "doc.md")

    assert _diff_only_report(["doc.md"], _TERMS, repo) == []


def test_diff_only_report_carves_out_email_only_added_line(tmp_path: Path) -> None:
    # The added-line scan applies the same email carve-out the full scan does:
    # a term that appears ONLY inside an author email address is not a leak.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    doc = repo / "doc.md"
    doc.write_text("Author: someone <dev@acme.example>\n", encoding="utf-8")
    _git(repo, "add", "doc.md")

    assert _diff_only_report(["doc.md"], _TERMS, repo) == []


def test_diff_only_report_falls_back_to_full_scan_when_diff_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When staged_added_lines returns None (could not resolve the staged diff),
    # the per-file fallback scans the whole file — fail closed, the banned term
    # is still caught. Drive the None branch directly so the fallback is
    # exercised regardless of the ambient git state.
    monkeypatch.setattr(banned_terms_cli, "staged_added_lines", lambda _repo, _file: None)
    doc = tmp_path / "doc.md"
    doc.write_text("acme on a line in a file the diff could not resolve\n", encoding="utf-8")

    report = _diff_only_report([str(doc)], _TERMS, tmp_path)

    assert report[0] == f"BANNED TERM in {doc}:"
    assert any("acme on a line" in line for line in report)


def test_diff_only_report_fallback_skips_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fallback path (None diff) with a non-existent file → skipped, no report
    # entry, no crash.
    monkeypatch.setattr(banned_terms_cli, "staged_added_lines", lambda _repo, _file: None)

    assert _diff_only_report([str(tmp_path / "does-not-exist.md")], _TERMS, tmp_path) == []


def test_diff_only_report_fallback_clean_existing_file_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fallback path (None diff) with an EXISTING clean file → no hits, empty.
    monkeypatch.setattr(banned_terms_cli, "staged_added_lines", lambda _repo, _file: None)
    clean = tmp_path / "clean.md"
    clean.write_text("a perfectly clean file with no banned term\n", encoding="utf-8")

    assert _diff_only_report([str(clean)], _TERMS, tmp_path) == []


def test_full_file_report_clean_file_is_empty(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("nothing banned here at all\n", encoding="utf-8")

    assert _full_file_report([str(clean)], _TERMS) == []


def test_resolve_banned_terms_reads_the_db_list(tmp_path: Path) -> None:
    db = _seed_db(tmp_path, ["acme", "  ", "widget"])
    # Whitespace-only entries are dropped; the rest are stripped.
    assert resolve_banned_terms(db_path=db) == ("acme", "widget")


def test_resolve_banned_terms_raises_on_unset(tmp_path: Path) -> None:
    # A missing DB, a DB that omits banned_terms, and a wrong-typed value are ALL
    # "unset" — refused LOUD so a load bug never masquerades as a deliberate
    # empty list.
    with pytest.raises(BannedTermsUnsetError):
        resolve_banned_terms(db_path=tmp_path / "nope.sqlite3")

    empty_db = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(str(empty_db))
    conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(BannedTermsUnsetError):
        resolve_banned_terms(db_path=empty_db)


def test_resolve_banned_terms_explicit_empty_list_is_allowed(tmp_path: Path) -> None:
    # The deliberate no-terms choice: an explicit empty array is NOT unset.
    db = _seed_db(tmp_path, [])
    assert resolve_banned_terms(db_path=db) == ()


def test_resolve_banned_terms_env_wins_over_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _seed_db(tmp_path, ["from-db"])
    monkeypatch.setenv("T3_BANNED_TERMS", "acme, widget")
    assert resolve_banned_terms(db_path=db) == ("acme", "widget")


def test_load_allowlist_stays_empty_when_unset(tmp_path: Path) -> None:
    # The allowlist is OPTIONAL — an absent row defaults to empty, never raises.
    db = _seed_db(tmp_path, ["acme"])
    assert _load_allowlist(db) == ()


class TestMainUnsetVsEmpty:
    """``main`` WARNS-and-allows on an unset banned_terms by default; empty is a no-op.

    With the DB-home store, "unset" is no ``banned_terms`` row AND no
    ``T3_BANNED_TERMS`` env. By default this WARNS loud and exits 0 — an unset
    list is not a banned-term violation on a dev/solo box (#3247). Only a
    deployment that sets ``banned_terms_required`` keeps the fail-loud exit 2. An
    explicit empty list is the deliberate no-op.
    """

    @staticmethod
    def _empty_db(tmp_path: Path) -> Path:
        empty_db = tmp_path / "empty.sqlite3"
        conn = sqlite3.connect(str(empty_db))
        conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
        conn.commit()
        conn.close()
        return empty_db

    def test_unset_db_warns_and_allows_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("T3_BANNED_TERMS_REQUIRED", raising=False)
        monkeypatch.setenv("T3_CONFIG_DB", str(self._empty_db(tmp_path)))
        clean = tmp_path / "clean.md"
        clean.write_text("nothing\n", encoding="utf-8")
        assert main([str(clean)]) == 0
        assert "UNSET" in capsys.readouterr().err

    def test_unset_db_fails_closed_when_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("T3_BANNED_TERMS_REQUIRED", "1")
        monkeypatch.setenv("T3_CONFIG_DB", str(self._empty_db(tmp_path)))
        clean = tmp_path / "clean.md"
        clean.write_text("nothing\n", encoding="utf-8")
        assert main([str(clean)]) == 2
        assert "banned_terms is unset" in capsys.readouterr().err

    def test_explicit_empty_banned_terms_is_a_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, [])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        flagged = tmp_path / "doc.md"
        flagged.write_text("acme reference\n", encoding="utf-8")
        assert main([str(flagged)]) == 0


def test_full_file_report_flags_committed_line(tmp_path: Path) -> None:
    repo, _db = _init_repo_with_committed_banned_line(tmp_path)
    doc = repo / "doc.md"

    report = _full_file_report([str(doc)], _TERMS)

    assert report[0] == f"BANNED TERM in {doc}:"
    assert any("pre-existing committed line" in line for line in report)


def test_full_file_report_skips_missing_file(tmp_path: Path) -> None:
    assert _full_file_report([str(tmp_path / "missing.md")], _TERMS) == []
