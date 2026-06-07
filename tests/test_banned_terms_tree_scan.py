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
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli.banned_terms import banned_terms_app
from teatree.core import banned_terms_tree
from teatree.hooks import banned_terms_tree_scan

# Synthetic high-confidence brand — never a real tenant name. Used so the
# pre-push banned-terms gate cannot trip on this test's own contents.
SYNTH_BRAND = "zzsynthbrand"


@pytest.fixture(autouse=True)
def _clear_brands_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any ambient brand env so tests start from a clean source."""
    monkeypatch.delenv("TEATREE_BANNED_BRANDS", raising=False)


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


def _config(tmp_path: Path, *, brands: list[str], banned_terms: list[str] | None = None) -> Path:
    cfg = tmp_path / ".teatree.toml"
    lines = ["[teatree]"]
    lines.append("banned_brands = [" + ", ".join(f'"{b}"' for b in brands) + "]")
    if banned_terms is not None:
        lines.append("banned_terms = [" + ", ".join(f'"{t}"' for t in banned_terms) + "]")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


class TestBuildBrandPattern:
    def test_empty_terms_returns_none(self) -> None:
        assert banned_terms_tree_scan.build_brand_pattern(()) is None

    def test_underscore_joined_prefix_is_matched(self) -> None:
        # The exact shape the \b matcher misses: a _ precedes the brand.
        pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
        assert pattern is not None
        assert pattern.search(f"wt_777_{SYNTH_BRAND}") is not None

    def test_underscore_joined_suffix_is_matched(self) -> None:
        pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
        assert pattern is not None
        assert pattern.search(f"{SYNTH_BRAND}_777") is not None

    def test_plain_word_boundary_still_matches(self) -> None:
        pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
        assert pattern is not None
        assert pattern.search(f"ship to {SYNTH_BRAND} today") is not None

    def test_substring_inside_a_larger_word_is_not_matched(self) -> None:
        # The brand must still be a token, not an arbitrary substring: a
        # letter glued directly to it (no joiner, no boundary) is NOT a hit.
        pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
        assert pattern is not None
        assert pattern.search(f"x{SYNTH_BRAND}y") is None

    def test_match_is_case_insensitive(self) -> None:
        pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
        assert pattern is not None
        assert pattern.search(SYNTH_BRAND.upper()) is not None


class TestScanTextEmailCarveOut:
    def test_brand_only_inside_email_is_allowed(self) -> None:
        pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
        assert pattern is not None
        hits = banned_terms_tree_scan.scan_text(f"ping dev@{SYNTH_BRAND}.com please", pattern)
        assert hits == []

    def test_brand_outside_email_on_same_line_is_flagged(self) -> None:
        pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
        assert pattern is not None
        hits = banned_terms_tree_scan.scan_text(f"{SYNTH_BRAND} ships; mail dev@{SYNTH_BRAND}.com", pattern)
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

    def test_no_brands_configured_is_clean(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        assert banned_terms_tree_scan.scan_tree(repo, ()) == []

    def test_untracked_file_is_not_scanned(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        (repo / "leak.py").write_text(f"x = '{SYNTH_BRAND}'\n", encoding="utf-8")  # not added
        assert banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,)) == []

    def test_binary_suffix_is_skipped(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "logo.png", f"binary-ish {SYNTH_BRAND}\n")
        assert banned_terms_tree_scan.scan_tree(repo, (SYNTH_BRAND,)) == []

    def test_non_repo_path_is_clean(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "app.py").write_text(f"x = '{SYNTH_BRAND}'\n", encoding="utf-8")
        assert banned_terms_tree_scan.scan_tree(plain, (SYNTH_BRAND,)) == []

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


class TestLoadBrandTerms:
    def test_reads_high_confidence_brands(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, brands=[SYNTH_BRAND])
        assert banned_terms_tree_scan.load_brand_terms(cfg) == (SYNTH_BRAND,)

    def test_common_word_banned_terms_are_not_read_as_brands(self, tmp_path: Path) -> None:
        # The flat common-word list stays out of the underscore-tolerant
        # brand scan, so a common word is never substring-matched.
        cfg = _config(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        assert banned_terms_tree_scan.load_brand_terms(cfg) == (SYNTH_BRAND,)

    def test_missing_config_is_empty(self, tmp_path: Path) -> None:
        assert banned_terms_tree_scan.load_brand_terms(tmp_path / "absent.toml") == ()

    def test_env_var_takes_precedence_over_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _config(tmp_path, brands=["fromconfig"])
        monkeypatch.setenv("TEATREE_BANNED_BRANDS", f" {SYNTH_BRAND} , other ")
        assert banned_terms_tree_scan.load_brand_terms(cfg) == (SYNTH_BRAND, "other")

    def test_env_var_supplies_brands_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_BANNED_BRANDS", SYNTH_BRAND)
        assert banned_terms_tree_scan.load_brand_terms(tmp_path / "absent.toml") == (SYNTH_BRAND,)

    def test_malformed_toml_is_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("not = valid = toml", encoding="utf-8")
        assert banned_terms_tree_scan.load_brand_terms(cfg) == ()

    def test_non_list_brands_key_is_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text('[teatree]\nbanned_brands = "not-a-list"\n', encoding="utf-8")
        assert banned_terms_tree_scan.load_brand_terms(cfg) == ()


class TestCommonWordIsNotSubstringMatched:
    def test_common_word_in_brand_scan_does_not_substring_match(self, tmp_path: Path) -> None:
        # If a common word like "ship" were ever fed to the brand scanner,
        # the token boundaries still prevent substring noise inside
        # "relationship" — and crucially the loader keeps it out entirely.
        cfg = _config(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        brands = banned_terms_tree_scan.load_brand_terms(cfg)
        repo = _repo_with(tmp_path, "src/app.py", "relationship = True\n")
        assert banned_terms_tree_scan.scan_tree(repo, brands) == []


class TestScanCommittedTree:
    def test_explicit_config_drives_the_scan(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, brands=[SYNTH_BRAND])
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = banned_terms_tree.scan_committed_tree(repo, config_path=cfg)
        assert [f.path for f in result.findings] == ["src/app.py"]
        assert result.brands_configured is True

    def test_env_var_brands_without_a_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_BANNED_BRANDS", SYNTH_BRAND)
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        result = banned_terms_tree.scan_committed_tree(repo)
        assert len(result.findings) == 1
        assert result.brands_configured is True

    def test_no_brands_anywhere_reports_inert(self, tmp_path: Path) -> None:
        # The brand backstop is INERT when no brands are configured: the
        # result carries findings (terminology only) AND the loud inert flag,
        # never a silent clean result that hides the unpopulated key.
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        result = banned_terms_tree.scan_committed_tree(repo, config_path=tmp_path / "absent.toml")
        assert result.findings == []
        assert result.brands_configured is False


class TestScanTreeCli:
    def test_dirty_tree_exits_nonzero_and_names_the_file(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, brands=[SYNTH_BRAND])
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(cfg)],
        )
        assert result.exit_code == 1
        assert "src/app.py" in result.stdout

    def test_clean_tree_exits_zero(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, brands=[SYNTH_BRAND])
        repo = _repo_with(tmp_path, "src/app.py", "WORKTREE = 'wt_777_generic'\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(cfg)],
        )
        assert result.exit_code == 0
        assert "clean" in result.stdout

    def test_env_var_brand_list_blocks_without_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The CI path: no ~/.teatree.toml, brand list comes from the env.
        monkeypatch.setenv("TEATREE_BANNED_BRANDS", SYNTH_BRAND)
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(tmp_path / "absent.toml")],
        )
        assert result.exit_code == 1
        assert "src/app.py" in result.stdout

    def test_no_config_exits_zero(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", f"x = '{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(tmp_path / "absent.toml")],
        )
        # An absent config file yields an empty brand list — a legitimate
        # no-op for the public repo, but the inert state must be LOUD, never
        # a silent clean green.
        assert result.exit_code == 0
        assert "INERT" in result.stdout
        assert "banned_brands" in result.stdout


class TestScanTreeCliInertSignal:
    """The brand backstop announces when it is INERT (#1591).

    An unpopulated ``banned_brands`` key is the defect #1591 fixes: the
    full-tree brand scan silently returned 0, hiding that the backstop did
    nothing. The CLI must emit a LOUD inert warning instead of a silent
    clean line, while still exiting 0 (the no-brands state is legitimate
    for the public repo).
    """

    def test_no_brands_emits_loud_inert_warning(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(tmp_path / "absent.toml")],
        )
        assert result.exit_code == 0
        assert "INERT" in result.stdout
        assert "banned_brands" in result.stdout
        # The silent-success phrasing must NOT be the whole story.
        assert "clean (0 findings)" not in result.stdout

    def test_empty_brands_list_in_config_is_inert(self, tmp_path: Path) -> None:
        # A config that declares banned_terms but leaves banned_brands empty
        # is exactly the #1591 scenario: the populated key is the wrong one.
        cfg = _config(tmp_path, brands=[], banned_terms=["ship", "delivery"])
        repo = _repo_with(tmp_path, "src/app.py", "clean = True\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(cfg)],
        )
        assert result.exit_code == 0
        assert "INERT" in result.stdout

    def test_populated_brands_does_not_warn_inert(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, brands=[SYNTH_BRAND])
        repo = _repo_with(tmp_path, "src/app.py", "WORKTREE = 'wt_777_generic'\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(cfg)],
        )
        assert result.exit_code == 0
        assert "INERT" not in result.stdout
        assert "clean" in result.stdout


class TestBackstopBrandVsCommonWord:
    """The activated backstop flags a planted brand but not a common word (#1591).

    The false-positive guard the curation enforces: a high-confidence brand
    in ``banned_brands`` is flagged across the whole tree, while a common
    word that lives ONLY in ``banned_terms`` (the point-of-egress list) is
    never fed to the underscore-tolerant tree scan, so it cannot substring-
    match across committed files.
    """

    def test_planted_brand_is_flagged_common_word_is_not(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, brands=[SYNTH_BRAND], banned_terms=["ship"])
        repo = _repo_with(
            tmp_path,
            "src/app.py",
            f"BRAND = 'wt_777_{SYNTH_BRAND}'\nNOTE = 'we ship relationships daily'\n",
        )
        result = banned_terms_tree.scan_committed_tree(repo, config_path=cfg)
        flagged_terms = {f.term.lower() for f in result.findings}
        assert SYNTH_BRAND in flagged_terms
        assert "ship" not in flagged_terms
        assert result.brands_configured is True


class TestScanTreeCliSummaryIsBrandAgnostic:
    """The summary describes findings generically, not as brand-only (#1736).

    ``scan-tree`` returns brand AND terminology findings; the summary line
    and remediation must not call a terminology finding a "brand" one.
    """

    def test_terminology_only_summary_does_not_say_brand(self, tmp_path: Path) -> None:
        # A conflated-terminology hit whose ONLY finding is a terminology
        # violation must never be labelled a "brand" finding in the count or
        # remediation lines. A non-matching brand is configured so the brand
        # backstop is active (no inert warning), isolating this assertion to
        # the finding-summary wording. The conflated phrase is assembled at
        # runtime so this (non-exempt) test file's own committed source never
        # trips the terminology backstop.
        conflated = "claude-" + "code " + "todos"
        cfg = _config(tmp_path, brands=[SYNTH_BRAND])
        repo = _repo_with(tmp_path, "docs/note.md", f"tracking {conflated} here\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(cfg)],
        )
        assert result.exit_code == 1
        assert "docs/note.md" in result.stdout
        assert "INERT" not in result.stdout
        assert "brand" not in result.stdout.lower()

    def test_summary_counts_findings_generically(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, brands=[SYNTH_BRAND])
        repo = _repo_with(tmp_path, "src/app.py", f"WORKTREE = 'wt_777_{SYNTH_BRAND}'\n")
        result = CliRunner().invoke(
            banned_terms_app,
            ["scan-tree", "--repo-root", str(repo), "--config", str(cfg)],
        )
        assert result.exit_code == 1
        assert "banned-term finding(s)" in result.stdout
        assert "brand-name finding" not in result.stdout


@pytest.mark.parametrize("joined", ["wt_777_{b}", "{b}_777", "a_{b}_z"])
def test_all_underscore_shapes_are_caught(joined: str) -> None:
    pattern = banned_terms_tree_scan.build_brand_pattern((SYNTH_BRAND,))
    assert pattern is not None
    assert pattern.search(joined.format(b=SYNTH_BRAND)) is not None
