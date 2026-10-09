r"""Tests for the full-tree banned-brand backstop scan (#1570).

The diff/payload gate (``banned_terms_scanner``) only sees a *change*; a
brand name ALREADY committed never appears in a post-landing diff. This
backstop enumerates every git-tracked file and scans its content for the
high-confidence brand list, with underscore-tolerant matching so a brand
glued into ``wt_777_<brand>`` is caught where the shell gate's ``\b``
matcher misses it.

No real customer/tenant brand name appears anywhere in this file — the
matching logic is exercised with the SYNTHETIC high-confidence term
``zzsynthbrand``. A common-word entry is exercised with ``ship`` to prove
the underscore tolerance is NOT applied to it (no substring noise).

The brand list is DB-home: tests seed a ``teatree_config_setting`` sqlite DB
and point the reader at it via ``db_path`` (direct calls) or ``T3_CONFIG_DB``
(the CliRunner in-process path).
"""

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from teatree.cli import banned_terms as banned_terms_cli
from teatree.cli.banned_terms import banned_terms_app
from teatree.core import banned_terms_tree
from teatree.hooks import banned_terms_tree_scan
from teatree.hooks.banned_terms_cli import resolve_banned_terms
from tests._ansi import strip_ansi

# Synthetic high-confidence brand — never a real tenant name. Used so the
# pre-push banned-terms gate cannot trip on this test's own contents.
SYNTH_BRAND = "zzsynthbrand"


@pytest.fixture(autouse=True)
def _clear_brands_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any ambient brand env so tests start from a clean source."""
    monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
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


def _repo_with(tmp_path: Path, relpath: str, content: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _seed_db(tmp_path: Path, *, brands: list[str], banned_terms: list[str] | None = None) -> Path:
    """Build a ``teatree_config_setting`` DB carrying the ``banned_term_registry`` row."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    registry = {"leak": brands, "prose_collider": banned_terms or []}
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
        (json.dumps(registry),),
    )
    conn.commit()
    conn.close()
    return db


def _replace_registry(db: Path, registry: dict[str, list[str]]) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE teatree_config_setting SET value=? WHERE key='banned_term_registry'", (json.dumps(registry),))
    conn.commit()
    conn.close()


def _empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
    conn.commit()
    conn.close()
    return db


class TestScanTextSharedMatcher:
    r"""The brand pass routes through the shared ``term_match`` matcher (fix #1).

    ``scan_text`` now takes the term tuple directly (no private regex) and
    matches whole tokens with ``-``/``_``/whitespace/camelCase separators,
    so a brand glued into an identifier — and a camelCase one the old
    ``\b`` regex missed — is caught while a substring is not.
    """

    def test_empty_terms_is_clean(self) -> None:
        assert banned_terms_tree_scan.scan_text(f"a {SYNTH_BRAND} line", ()) == []

    def test_underscore_joined_prefix_is_matched(self) -> None:
        # The exact shape the \b matcher misses: a _ precedes the brand.
        hits = banned_terms_tree_scan.scan_text(f"wt_777_{SYNTH_BRAND}", (SYNTH_BRAND,))
        assert [h[1].lower() for h in hits] == [SYNTH_BRAND]

    def test_underscore_joined_suffix_is_matched(self) -> None:
        hits = banned_terms_tree_scan.scan_text(f"{SYNTH_BRAND}_777", (SYNTH_BRAND,))
        assert [h[1].lower() for h in hits] == [SYNTH_BRAND]

    def test_plain_word_boundary_still_matches(self) -> None:
        hits = banned_terms_tree_scan.scan_text(f"ship to {SYNTH_BRAND} today", (SYNTH_BRAND,))
        assert [h[1].lower() for h in hits] == [SYNTH_BRAND]

    def test_camelcase_identifier_is_matched(self) -> None:
        # The shared matcher splits camelCase — the old \b regex did NOT, so
        # this is the parity gap fix #1 closes.
        camel = SYNTH_BRAND.capitalize() + "Config"
        hits = banned_terms_tree_scan.scan_text(f"value = {camel}", (SYNTH_BRAND,))
        assert [h[1].lower() for h in hits] == [SYNTH_BRAND]

    def test_substring_inside_a_larger_word_is_not_matched(self) -> None:
        # The brand must still be a whole token, not an arbitrary substring: a
        # letter glued directly to it (no separator, no case boundary) is NOT a hit.
        assert banned_terms_tree_scan.scan_text(f"x{SYNTH_BRAND}y", (SYNTH_BRAND,)) == []

    def test_match_is_case_insensitive(self) -> None:
        hits = banned_terms_tree_scan.scan_text(SYNTH_BRAND.upper(), (SYNTH_BRAND,))
        assert len(hits) == 1


class TestScanTextScansEmailAddresses:
    """An address is published text, so a brand inside one is a hit (not carved out)."""

    def test_brand_only_inside_email_is_flagged(self) -> None:
        hits = banned_terms_tree_scan.scan_text(f"ping dev@{SYNTH_BRAND}.com please", (SYNTH_BRAND,))
        assert len(hits) == 1

    def test_address_with_no_brand_is_clean(self) -> None:
        assert banned_terms_tree_scan.scan_text("ping dev@example.org please", (SYNTH_BRAND,)) == []

    def test_brand_outside_email_on_same_line_is_flagged(self) -> None:
        hits = banned_terms_tree_scan.scan_text(f"{SYNTH_BRAND} ships; mail dev@{SYNTH_BRAND}.com", (SYNTH_BRAND,))
        assert len(hits) == 1
        assert hits[0][0] == 1


class TestScanTree:
    def test_underscore_joined_brand_in_tree_is_caught(self, tmp_path: Path) -> None:
        # RED→GREEN: the \b matcher misses the _-joined prefix; the
        # underscore-tolerant tree scan catches it.
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        findings = banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,))
        assert len(findings) == 1
        assert findings[0].path == "src/app.py"
        assert findings[0].lineno == 1
        assert findings[0].term.lower() == SYNTH_BRAND

    def test_old_word_boundary_matcher_would_miss_underscore_prefix(self) -> None:
        # Proves the bug the backstop fixes: the legacy \b(term)\b pattern
        # never matches a brand preceded by an underscore.
        legacy = re.compile(rf"\b({re.escape(SYNTH_BRAND)})\b", re.IGNORECASE)
        assert legacy.search(f"wt_777_{SYNTH_BRAND}") is None

    def test_clean_tree_returns_no_findings(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", "WORKTREE = 'wt_777_generic'\n")
        assert banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,)) == []

    def test_no_brands_configured_refuses_scan(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        with pytest.raises(banned_terms_tree_scan.BannedTermsUnsetError, match=r"banned_term_registry\.leak is empty"):
            banned_terms_tree_scan.scan_tree(repo, ())

    def test_untracked_file_is_not_scanned(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        (repo / "leak.py").write_text(f"x = '{SYNTH_BRAND}'\n", encoding="utf-8")  # not added
        assert banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,)) == []

    def test_binary_suffix_is_skipped(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "logo.png", f"binary-ish {SYNTH_BRAND}\n")
        assert banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,)) == []

    def test_non_repo_path_refuses_rather_than_reporting_clean(self, tmp_path: Path) -> None:
        # #4354 §2: enumerating nothing is a NON-ANSWER. Returning [] here made the
        # brand-leak backstop report "clean (0 findings)" on a tree it never read.
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "app.py").write_text(f"x = '{SYNTH_BRAND}'\n", encoding="utf-8")
        with pytest.raises(banned_terms_tree_scan.TreeEnumerationError):
            banned_terms_tree_scan.scan_tree(plain, (SYNTH_BRAND,))

    def test_undecodable_text_file_is_skipped(self, tmp_path: Path) -> None:
        # A tracked .py with invalid UTF-8 bytes cannot be read — the scan
        # skips it (fail-open) rather than crashing.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        (repo / "blob.py").write_bytes(b"\xff\xfe" + SYNTH_BRAND.encode() + b"\xff")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "seed")
        assert banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,)) == []


class TestScanTreeReadsCommittedBlob:
    """Fix #5: the scan reads the COMMITTED blob, not the working tree.

    A staged/working-tree edit that removes a brand from the file but
    leaves it in the last commit must NOT hide the committed leak from the
    backstop — the whole point of a *committed*-content backstop. So the
    scan reads ``git show HEAD:<path>`` and only falls back to the working
    tree for a not-yet-committed file.
    """

    def test_working_tree_edit_does_not_hide_committed_brand(self, tmp_path: Path) -> None:
        # Commit a brand, then scrub it from the WORKING TREE only (no new
        # commit). The committed blob still carries the brand, so the scan
        # still catches it — a working-tree-only edit cannot launder it.
        repo = _repo_with(tmp_path, "src/app.py", f"BRAND = '{SYNTH_BRAND}'\n")
        (repo / "src/app.py").write_text("BRAND = 'generic'\n", encoding="utf-8")
        findings = banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,))
        assert len(findings) == 1
        assert findings[0].path == "src/app.py"
        assert findings[0].term.lower() == SYNTH_BRAND

    def test_staged_only_clean_edit_does_not_hide_committed_brand(self, tmp_path: Path) -> None:
        # Same defect via the staging area: stage a clean version but never
        # commit it. The HEAD blob still leaks, so the scan still flags it.
        repo = _repo_with(tmp_path, "src/app.py", f"BRAND = '{SYNTH_BRAND}'\n")
        (repo / "src/app.py").write_text("BRAND = 'generic'\n", encoding="utf-8")
        _git(repo, "add", "-A")
        findings = banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,))
        assert [f.path for f in findings] == ["src/app.py"]

    def test_working_tree_brand_absent_from_commit_is_caught_via_fallback(self, tmp_path: Path) -> None:
        # The complement: a freshly-added, NOT-yet-committed tracked file has
        # no HEAD blob, so the scan falls back to the working-tree content and
        # still catches a brand introduced there.
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        (repo / "src/new.py").write_text(f"BRAND = '{SYNTH_BRAND}'\n", encoding="utf-8")
        _git(repo, "add", "src/new.py")  # tracked (staged) but not committed
        findings = banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,))
        assert [f.path for f in findings] == ["src/new.py"]

    def test_committed_blob_text_returns_head_content(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", f"BRAND = '{SYNTH_BRAND}'\n")
        (repo / "src/app.py").write_text("BRAND = 'generic'\n", encoding="utf-8")
        blob = banned_terms_tree_scan.committed_blob_text(repo, "src/app.py")
        assert blob is not None
        assert SYNTH_BRAND in blob

    def test_committed_blob_text_is_none_for_uncommitted_path(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        (repo / "src/new.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "src/new.py")
        assert banned_terms_tree_scan.committed_blob_text(repo, "src/new.py") is None


class TestScanTreeOfAVendoredSubtree:
    """A root that is a subdirectory of its repo reads ITS OWN committed blobs.

    ``git show HEAD:<path>`` resolves the path from the repository top, whatever ``-C``
    names. Scanning a vendored ``vendor/<core>`` therefore read the enclosing repo's
    same-named files (``pyproject.toml``, ``.gitignore``, ...) and reported their text
    under the vendored path, while the vendored file itself went unread.
    """

    def _fork(self, tmp_path: Path, *, root_text: str, vendored_text: str) -> Path:
        fork = _repo_with(tmp_path, "pyproject.toml", root_text)
        vendored = fork / "vendor" / "core"
        vendored.mkdir(parents=True)
        (vendored / "pyproject.toml").write_text(vendored_text, encoding="utf-8")
        _git(fork, "add", "-A")
        _git(fork, "commit", "-m", "vendor core")
        return vendored

    def test_the_enclosing_repos_same_named_file_is_not_reported(self, tmp_path: Path) -> None:
        vendored = self._fork(tmp_path, root_text=f"name = '{SYNTH_BRAND}'\n", vendored_text="name = 'core'\n")
        assert banned_terms_tree_scan.scan_tree(vendored, (SYNTH_BRAND,)) == []

    def test_the_vendored_files_own_committed_brand_is_found(self, tmp_path: Path) -> None:
        vendored = self._fork(tmp_path, root_text="name = 'fork'\n", vendored_text=f"name = '{SYNTH_BRAND}'\n")
        assert [f.path for f in banned_terms_tree_scan.scan_tree(vendored, (SYNTH_BRAND,))] == ["pyproject.toml"]


class TestLoadBrandTerms:
    """``load_brand_terms`` requires a populated leak list.

    An absent, malformed, or empty leak class raises ``BannedTermsUnsetError``.
    ``$TEATREE_TERM_REGISTRY`` keeps precedence and short-circuits the raise.
    """

    def test_reads_high_confidence_brands(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        assert banned_terms_tree_scan.load_brand_terms(db_path=db) == (SYNTH_BRAND,)

    def test_common_word_banned_terms_are_not_read_as_brands(self, tmp_path: Path) -> None:
        # The flat common-word list stays out of the underscore-tolerant
        # brand scan, so a common word is never substring-matched.
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        assert banned_terms_tree_scan.load_brand_terms(db_path=db) == (SYNTH_BRAND,)

    def test_explicit_empty_brands_list_is_refused(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path, brands=[])
        with pytest.raises(banned_terms_tree_scan.BannedTermsUnsetError, match="no terms for the tree scan"):
            banned_terms_tree_scan.load_brand_terms(db_path=db)

    def test_missing_db_with_no_env_raises(self, tmp_path: Path) -> None:
        with pytest.raises(banned_terms_tree_scan.BannedTermsUnsetError):
            banned_terms_tree_scan.load_brand_terms(db_path=tmp_path / "absent.sqlite3")

    def test_a_registry_without_a_leak_class_refuses(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        _replace_registry(db, {"prose_collider": ["ship"]})
        with pytest.raises(banned_terms_tree_scan.BannedTermsUnsetError, match="no terms for the tree scan"):
            banned_terms_tree_scan.load_brand_terms(db_path=db)

    def test_a_registry_holding_only_leak_terms_still_enforces_them(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        _replace_registry(db, {"leak": [SYNTH_BRAND]})
        assert banned_terms_tree_scan.load_brand_terms(db_path=db) == (SYNTH_BRAND,)
        assert SYNTH_BRAND in resolve_banned_terms(db_path=db)

    def test_no_banned_brands_row_raises(self, tmp_path: Path) -> None:
        with pytest.raises(banned_terms_tree_scan.BannedTermsUnsetError):
            banned_terms_tree_scan.load_brand_terms(db_path=_empty_db(tmp_path))

    def test_env_var_takes_precedence_over_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, brands=["fromdb"])
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [SYNTH_BRAND, "other"], "prose_collider": []}))
        assert banned_terms_tree_scan.load_brand_terms(db_path=db) == (SYNTH_BRAND, "other")

    def test_env_var_supplies_brands_without_a_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [SYNTH_BRAND], "prose_collider": []}))
        assert banned_terms_tree_scan.load_brand_terms(db_path=tmp_path / "absent.sqlite3") == (SYNTH_BRAND,)

    def test_corrupt_db_value_raises(self, tmp_path: Path) -> None:
        # A non-JSON stored value fails open to None in the cold reader, which is
        # the load-bug-shaped UNSET → refused LOUD, never scanned as empty.
        db = _empty_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)", ("{",)
        )
        conn.commit()
        conn.close()
        with pytest.raises(banned_terms_tree_scan.BannedTermsUnsetError):
            banned_terms_tree_scan.load_brand_terms(db_path=db)

    def test_non_list_brands_value_raises(self, tmp_path: Path) -> None:
        db = _empty_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
            (json.dumps({"leak": "not-a-list", "prose_collider": []}),),
        )
        conn.commit()
        conn.close()
        with pytest.raises(banned_terms_tree_scan.BannedTermsUnsetError):
            banned_terms_tree_scan.load_brand_terms(db_path=db)


class TestBannedTermsUnsetErrorMessageIsKeyAware:
    """``for_key`` phrases the message for the actual key it is raised for.

    The registry message names the required leak class and the JSON secret.
    """

    def test_registry_message_names_required_classes(self) -> None:
        message = str(
            banned_terms_tree_scan.BannedTermsUnsetError.for_key("banned_term_registry", "TEATREE_TERM_REGISTRY")
        )
        assert "banned_term_registry is unset" in message
        assert "nonempty leak list" in message
        assert "$TEATREE_TERM_REGISTRY" in message
        assert "no terms" not in message


class TestCommonWordIsNotSubstringMatched:
    def test_common_word_in_brand_scan_does_not_substring_match(self, tmp_path: Path) -> None:
        # If a common word like "ship" were ever fed to the brand scanner,
        # the token boundaries still prevent substring noise inside
        # "relationship" — and crucially the loader keeps it out entirely.
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        brands = banned_terms_tree_scan.load_brand_terms(db_path=db)
        repo = _repo_with(tmp_path, "src/app.py", "relationship = True\n")
        assert banned_terms_tree_scan.scan_tree(repo, brands) == []


class TestScanCommittedTree:
    def test_explicit_db_drives_the_scan(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = banned_terms_tree.scan_committed_tree(repo, config_path=db)
        assert [f.path for f in result.findings] == ["src/app.py"]

    def test_env_var_brands_without_a_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [SYNTH_BRAND], "prose_collider": []}))
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        result = banned_terms_tree.scan_committed_tree(repo)
        assert len(result.findings) == 1

    def test_no_brands_anywhere_raises(self, tmp_path: Path) -> None:
        # Genuinely unset (no DB row, no env) is refused LOUD by the loader and
        # propagated by the coordinator, never a silent inert scan that hides a
        # load bug.
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        with pytest.raises(banned_terms_tree.BannedTermsUnsetError):
            banned_terms_tree.scan_committed_tree(repo, config_path=tmp_path / "absent.sqlite3")

    def test_explicit_empty_brands_refuses_before_scanning(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path, brands=[], banned_terms=["ship"])
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        with pytest.raises(banned_terms_tree.BannedTermsUnsetError, match="no terms for the tree scan"):
            banned_terms_tree.scan_committed_tree(repo, config_path=db)


class TestScanTreeCli:
    def test_dirty_tree_exits_nonzero_and_names_the_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 1
        assert "src/app.py" in result.stdout

    def test_clean_tree_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "src/app.py", "WORKTREE = 'wt_777_generic'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 0
        assert "clean" in result.stdout

    def test_env_var_brand_list_blocks_without_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The CI path: no DB row, brand list comes from the env.
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [SYNTH_BRAND], "prose_collider": []}))
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 1
        assert "src/app.py" in result.stdout

    def test_no_db_with_no_env_exits_misconfigured(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        # Genuinely-unset brands (no DB row, no env) FAIL LOUD (exit 2): an
        # unset list is too dangerous to scan as empty — a load bug would look
        # identical to a deliberate no-brands choice.
        assert result.exit_code == 2
        assert "MISCONFIGURED" in result.stdout
        assert "unset" in result.stdout.lower()


class TestScanTreeCliInertSignal:
    """Missing or empty brand lists both fail loud.

    The #1591 design announced an INERT backstop but still exited 0 for BOTH
    a genuinely-absent ``banned_brands`` and a deliberate empty list — the
    exact conflation this rule forbids. Both now hard-fail (exit 2).
    """

    def test_unset_brands_exits_misconfigured(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 2
        assert "MISCONFIGURED" in result.stdout
        assert "unset" in result.stdout.lower()

    def test_empty_brands_list_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, brands=[], banned_terms=["ship", "delivery"])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 2
        assert "MISCONFIGURED" in result.stdout

    def test_populated_brands_does_not_warn_inert(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "src/app.py", "WORKTREE = 'wt_777_generic'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 0
        assert "INERT" not in result.stdout
        assert "clean" in result.stdout


class TestScanTreeRequiredBrands:
    """A populated leak class is required for every full-tree scan."""

    def test_brands_configured_runs_normally(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "src/app.py", "WORKTREE = 'wt_777_generic'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 0
        assert "clean" in result.stdout

    def test_brands_still_reports_findings_as_exit_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A dirty tree is exit 1 (findings), NOT exit 2 —
        # the two failure modes stay distinct.
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 1
        assert "src/app.py" in result.stdout


class TestEnumerationFailureIsLoud:
    """A scan that enumerated ZERO files is a non-answer, never "clean" (#4354 §2).

    ``git_tracked_files`` mapped every failure — a non-repo cwd, a missing git, a
    ``safe.directory`` refusal, an ``ls-files`` timeout — onto an empty list, and zero
    files scanned produced zero findings and a green "clean (0 findings)". The gate
    exists to stop an operator brand reaching a PUBLIC repo, so its only sound answer
    to "I could not read the tree" is a loud MISCONFIGURED.
    """

    def test_enumerating_a_non_repo_raises(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(banned_terms_tree_scan.TreeEnumerationError):
            banned_terms_tree_scan.git_tracked_files(plain)

    def test_a_repo_with_no_tracked_files_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        with pytest.raises(banned_terms_tree_scan.TreeEnumerationError):
            banned_terms_tree_scan.git_tracked_files(repo)

    def test_cli_exits_misconfigured_and_never_prints_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [SYNTH_BRAND], "prose_collider": []}))
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "leak.md").write_text(f"{SYNTH_BRAND} leaked\n", encoding="utf-8")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(plain)])
        assert result.exit_code == 2
        assert "MISCONFIGURED" in strip_ansi(result.stdout)
        assert "clean (0 findings)" not in strip_ansi(result.stdout)

    def test_control_the_same_file_inside_a_repo_is_a_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: the ONLY difference is the enclosing git repo, and it exits 1."""
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [SYNTH_BRAND], "prose_collider": []}))
        repo = _repo_with(tmp_path, "leak.md", f"{SYNTH_BRAND} leaked\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        assert result.exit_code == 1
        assert "leak.md" in strip_ansi(result.stdout)


class TestBannedTermsTreeCiUsesUnconditionalGate:
    """The CI job uses the same required scan as the local CLI."""

    def test_ci_step_runs_without_a_mode_flag(self) -> None:
        import yaml  # noqa: PLC0415

        ci = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text())
        steps = ci["jobs"]["banned-terms-tree"]["steps"]
        joined = " ".join(s.get("run", "") for s in steps if isinstance(s, dict))
        assert "scan-tree" in joined, "The banned-terms-tree CI step must run `banned-terms scan-tree`."
        assert "--require-brands" not in joined
        assert "--allow-unset" not in joined


class TestBackstopBrandVsCommonWord:
    """The activated backstop flags a planted brand but not a common word (#1591).

    The false-positive guard the curation enforces: a high-confidence brand
    in ``banned_brands`` is flagged across the whole tree, while a common
    word that lives ONLY in ``banned_terms`` (the point-of-egress list) is
    never fed to the underscore-tolerant tree scan, so it cannot substring-
    match across committed files.
    """

    def test_planted_brand_is_flagged_common_word_is_not(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        repo = _repo_with(
            tmp_path,
            "src/app.py",
            f"BRAND = 'wt_777_{SYNTH_BRAND}'\nNOTE = 'we ship relationships daily'\n",
        )
        result = banned_terms_tree.scan_committed_tree(repo, config_path=db)
        flagged_terms = {f.term.lower() for f in result.findings}
        assert SYNTH_BRAND in flagged_terms
        assert "ship" not in flagged_terms


class TestScanTreeCliSummaryIsBrandAgnostic:
    """The summary describes findings generically, not as brand-only (#1736).

    ``scan-tree`` returns brand AND terminology findings; the summary line
    and remediation must not call a terminology finding a "brand" one.
    """

    @pytest.fixture(autouse=True)
    def _force_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # banned_terms.py's `_console = Console()` is a module-level singleton;
        # its color_system is resolved once from os.environ at import time and
        # cached, so a runtime monkeypatch.setenv("FORCE_COLOR", ...) never
        # reaches it. Reconstruct the singleton with force_terminal pinned
        # directly instead, so the color-forced path is exercised on every
        # run (not just a dev shell that happens to set it) — an ANSI-wrapped
        # "docs/note.md" or "banned-term finding(s)" breaks a plain substring
        # match otherwise (souliane/teatree#2359).
        monkeypatch.setattr(banned_terms_cli, "_console", Console(force_terminal=True))

    def test_terminology_only_summary_does_not_say_brand(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A conflated-terminology hit whose ONLY finding is a terminology
        # violation must never be labelled a "brand" finding in the count or
        # remediation lines. A non-matching brand is configured so the brand
        # backstop is active (no inert warning), isolating this assertion to
        # the finding-summary wording. The conflated phrase is assembled at
        # runtime so this (non-exempt) test file's own committed source never
        # trips the terminology backstop.
        conflated = "claude-" + "code " + "todos"
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "docs/note.md", f"tracking {conflated} here\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        stdout = strip_ansi(result.stdout)
        assert result.exit_code == 1
        assert "docs/note.md" in stdout
        assert "INERT" not in stdout
        assert "brand" not in stdout.lower()

    def test_summary_counts_findings_generically(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _seed_db(tmp_path, brands=[SYNTH_BRAND])
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(banned_terms_app, ["scan-tree", "--repo-root", str(repo)])
        stdout = strip_ansi(result.stdout)
        assert result.exit_code == 1
        assert "banned-term finding(s)" in stdout
        assert "brand-name finding" not in stdout


@pytest.mark.parametrize("joined", ["wt_777_{b}", "{b}_777", "a_{b}_z"])
def test_all_underscore_shapes_are_caught(joined: str) -> None:
    hits = banned_terms_tree_scan.scan_text(joined.format(b=SYNTH_BRAND), (SYNTH_BRAND,))
    assert [h[1].lower() for h in hits] == [SYNTH_BRAND]


class TestTreeEnumerationFailsLoud:
    """An unread tree is refused, never reported as a clean one.

    ``git ls-files`` failing or timing out produced an empty file list, which the
    scan reported as zero findings — the same answer a genuinely clean repo
    gives. On a leak backstop that is the fail-open shape: the gate says "clean"
    about a tree it never opened.
    """

    def test_a_timed_out_enumeration_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo_with(tmp_path, "src/app.py", f"BRAND = '{SYNTH_BRAND}'\n")

        def _timeout(*_args: object, **_kwargs: object) -> object:
            raise banned_terms_tree_scan.TimeoutExpired(["git", "ls-files"], 30)

        monkeypatch.setattr(banned_terms_tree_scan, "run_allowed_to_fail", _timeout)
        with pytest.raises(banned_terms_tree_scan.TreeEnumerationError):
            banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,))

    def test_an_errored_enumeration_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo_with(tmp_path, "src/app.py", f"BRAND = '{SYNTH_BRAND}'\n")

        class _Failed:
            returncode = 128
            stdout = ""
            stderr = "fatal: index file corrupt"

        monkeypatch.setattr(banned_terms_tree_scan, "run_allowed_to_fail", lambda *_a, **_k: _Failed())
        with pytest.raises(banned_terms_tree_scan.TreeEnumerationError):
            banned_terms_tree_scan.git_tracked_files(repo)


class TestSuffixlessTrackedFilesAreScanned:
    """A tracked text file without a recognised suffix is still committed content.

    The scanner kept an ALLOWLIST of text suffixes, so every tracked file without
    one — ``Dockerfile``, ``NOTICE``, ``CODEOWNERS``, an extension-less script —
    was silently invisible to the backstop whose whole job is to see what the
    diff gate cannot.
    """

    @pytest.mark.parametrize("filename", ["Dockerfile", "NOTICE", "CODEOWNERS", "run-migrations"])
    def test_a_committed_brand_in_a_suffixless_file_is_found(self, tmp_path: Path, filename: str) -> None:
        repo = _repo_with(tmp_path, filename, f"MAINTAINER {SYNTH_BRAND}\n")
        findings = banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,))
        assert [f.path for f in findings] == [filename]

    def test_a_binary_suffix_is_still_skipped(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "logo.png", f"binary-ish {SYNTH_BRAND}\n")
        assert banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,)) == []
