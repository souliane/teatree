"""Tests for the §17.1 invariant-numbering integrity gate (#836 §17.6 gate 1).

Recurring collision class: concurrent PRs each appended "the next"
§17.1 invariant number against a stale base, so the merge silently
duplicated or dropped one (occurred 3x in one session: #856/#859,
#859/#863). This gate parses §17.1's numbered list and fails when the
numbers are not a gapless 1..N with no repeats, evaluated on whatever
tree (incl. merge result) is being committed.

`TestCiCrossPrDetection` covers the cross-PR mode (codex #1282 item 7,
souliane/teatree#1288): the pre-commit gate is tree-local and cannot
catch two concurrent PRs each adding the same §17.N against a stale
base. The CI mode reads the base ref's BLUEPRINT.md and fails when the
PR introduces invariant numbers that already exist on the base.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.hooks.check_blueprint_invariant_numbering import (
    check_numbering,
    check_numbering_against_base,
    ci_main,
    extract_invariant_numbers,
    main,
)

_CLEAN = """\
## 17. Factory

### 17.1 Invariants

1. **Two layers.** Substrate vs improvement.

2. **The flywheel.** Defect to enforcement.

3. **Topology.** Orchestrator brain.

### 17.2 The flywheel

1. This numbered item is in another subsection and must be ignored.
2. So is this one.
"""

_DUPLICATE = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

3. **Three.** c

4. **Four.** d

5. **Five.** e

6. **Six (PR A).** appended against stale base

6. **Six (PR B).** appended against the same stale base

### 17.2 Next
"""

_GAP = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

4. **Four.** a merge dropped invariant 3

### 17.2 Next
"""


class TestExtractInvariantNumbers:
    def test_reads_only_the_17_1_block(self) -> None:
        assert extract_invariant_numbers(_CLEAN) == [1, 2, 3]

    def test_captures_duplicates_in_order(self) -> None:
        assert extract_invariant_numbers(_DUPLICATE) == [1, 2, 3, 4, 5, 6, 6]

    def test_captures_gap(self) -> None:
        assert extract_invariant_numbers(_GAP) == [1, 2, 4]

    def test_no_section_yields_empty(self) -> None:
        assert extract_invariant_numbers("no invariants section here\n1. **x.** y") == []


class TestCheckNumbering:
    def test_clean_contiguous_is_ok(self) -> None:
        result = check_numbering([1, 2, 3, 4, 5])
        assert result.ok is True

    def test_duplicate_six_fails(self) -> None:
        result = check_numbering([1, 2, 3, 4, 5, 6, 6])
        assert result.ok is False
        assert "Duplicate" in result.reason
        assert "[6]" in result.reason

    def test_duplicate_seven_fails(self) -> None:
        result = check_numbering([1, 2, 3, 4, 5, 6, 7, 7, 8])
        assert result.ok is False
        assert "Duplicate" in result.reason

    def test_gap_fails(self) -> None:
        result = check_numbering([1, 2, 4])
        assert result.ok is False
        assert "not contiguous" in result.reason

    def test_empty_fails(self) -> None:
        result = check_numbering([])
        assert result.ok is False
        assert "No numbered invariants" in result.reason


class TestMainOnRealBlueprint:
    """Anti-vacuous: run the gate against the repo's real BLUEPRINT.md."""

    def test_repo_blueprint_17_1_is_contiguous(self) -> None:
        from scripts.hooks.check_blueprint_invariant_numbering import _blueprint_path  # noqa: PLC0415

        text = _blueprint_path().read_text(encoding="utf-8")
        result = check_numbering(extract_invariant_numbers(text))
        assert result.ok is True, result.reason


class TestMain:
    def test_noop_when_blueprint_not_in_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: False)
        assert main() == 0

    def test_fails_on_duplicate_when_committed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        bp = tmp_path / "BLUEPRINT.md"
        bp.write_text(_DUPLICATE, encoding="utf-8")
        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: True)
        monkeypatch.setattr(mod, "_blueprint_path", lambda: bp)
        assert main() == 1

    def test_fails_on_gap_when_committed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        bp = tmp_path / "BLUEPRINT.md"
        bp.write_text(_GAP, encoding="utf-8")
        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: True)
        monkeypatch.setattr(mod, "_blueprint_path", lambda: bp)
        assert main() == 1

    def test_passes_on_clean_when_committed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import scripts.hooks.check_blueprint_invariant_numbering as mod  # noqa: PLC0415

        bp = tmp_path / "BLUEPRINT.md"
        bp.write_text(_CLEAN, encoding="utf-8")
        monkeypatch.setattr(mod, "_blueprint_in_commit", lambda: True)
        monkeypatch.setattr(mod, "_blueprint_path", lambda: bp)
        assert main() == 0


_BASE_THREE = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

3. **Three.** c

### 17.2 Next
"""

# Two concurrent PRs: base has 1..3; PR A merges first, adding §17.4
# "Alpha". PR B branched from the same base, also adds §17.4 "Beta".
# Tree-local check passes for both PRs (each ships a clean 1..N
# sequence). Only a base-aware check catches the collision.
_BASE_AFTER_PR_A_MERGED = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

3. **Three.** c

4. **Alpha.** added by PR A, now on main

### 17.2 Next
"""

_PR_B_AGAINST_STALE_BASE = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

3. **Three.** c

4. **Beta.** PR B, branched from BASE_THREE (stale)

### 17.2 Next
"""

_PR_C_NEW_NUMBER = """\
### 17.1 Invariants

1. **One.** a

2. **Two.** b

3. **Three.** c

4. **Alpha.** already on main (PR A)

5. **Gamma.** PR C, rebased on top of PR A — no collision

### 17.2 Next
"""


_GIT_BIN = shutil.which("git") or "/usr/bin/git"


def _run_git(repo: Path, *args: str) -> str:
    """Run ``git`` inside *repo* with a sanitized env.

    The pre-commit pytest hook exports ``GIT_*`` vars from the outer
    ``git commit``; inheriting them would let the inner ``git`` calls
    hijack the real repo. See AGENTS.md § Test-Writing Doctrine.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.setdefault("GIT_AUTHOR_NAME", "test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    result = subprocess.run(
        [_GIT_BIN, *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class TestCheckNumberingAgainstBase:
    """Pure logic: given base + pr invariants, detect collisions."""

    def test_disjoint_additions_ok(self) -> None:
        # merge-base 1..3; base unchanged 1..3; pr adds 4 — no collision.
        result = check_numbering_against_base(base=[1, 2, 3], pr=[1, 2, 3, 4], merge_base=[1, 2, 3])
        assert result.ok is True

    def test_same_number_added_by_pr_and_base_fails(self) -> None:
        # merge-base 1..3; base advanced to 1..4 (PR A merged) while
        # this PR was in flight; PR also adds 4 from the stale base.
        result = check_numbering_against_base(base=[1, 2, 3, 4], pr=[1, 2, 3, 4], merge_base=[1, 2, 3])
        # Each side has a clean 1..N, but PR's 4 collides with base's 4.
        assert result.ok is False
        assert "4" in result.reason
        assert "base" in result.reason.lower() or "main" in result.reason.lower()

    def test_pr_post_rebase_no_collision(self) -> None:
        # Post-rebase the merge-base advances to wherever main was;
        # PR's 4 is now identical to main's 4 and PR adds 5 cleanly.
        result = check_numbering_against_base(base=[1, 2, 3, 4], pr=[1, 2, 3, 4, 5], merge_base=[1, 2, 3, 4])
        assert result.ok is True

    def test_pr_subset_no_added_numbers_ok(self) -> None:
        # PR doesn't touch §17.1 at all (same numbers as merge-base/base).
        result = check_numbering_against_base(base=[1, 2, 3], pr=[1, 2, 3], merge_base=[1, 2, 3])
        assert result.ok is True

    def test_pr_removes_a_number_ok(self) -> None:
        # PR retires an invariant (renumbering); not a collision.
        result = check_numbering_against_base(base=[1, 2, 3], pr=[1, 2], merge_base=[1, 2, 3])
        assert result.ok is True

    def test_fallback_no_merge_base_uses_common_prefix(self) -> None:
        # CLI / programmatic callers may not have a merge-base handy.
        # The fallback compares everything past the longest common
        # 1..k prefix — the conservative proxy.
        result = check_numbering_against_base(base=[1, 2, 3], pr=[1, 2, 3, 4])
        assert result.ok is True
        result = check_numbering_against_base(base=[1, 2, 3, 4], pr=[1, 2, 3])
        assert result.ok is True


class TestCiCrossPrDetection:
    """End-to-end with real git: simulate the cross-PR collision class.

    Builds a tmp repo with the merge-base BLUEPRINT.md, branches the
    PR off it, then advances ``base-ref`` (the "main" pointer) past
    the branch-point to simulate a concurrently-merged PR. ``ci_main``
    must:

    1. RED — fail when the PR's BLUEPRINT adds a §17.N that the
        advanced base also added (the cross-PR collision).
    2. GREEN — pass when the PR rebases and adds a strictly newer
        number.
    3. SKIP — no-op when BLUEPRINT.md isn't in the PR diff.
    """

    def _make_repo_with_branch_point(
        self,
        tmp_path: Path,
        *,
        ancestor: str,
        base_after_concurrent_merge: str | None = None,
    ) -> Path:
        """Build a repo. Branch off ``ancestor``, optionally advance base.

        - Commit 1: ``ancestor`` (BLUEPRINT.md). Tag as ``base-ref``.
        - Create branch ``pr-branch`` HERE (so the merge-base is this commit).
        - If ``base_after_concurrent_merge`` is set: switch to ``main``,
            commit it (simulating PR A merging into main), and update
            ``base-ref`` to point at that new tip. ``pr-branch`` still
            points at the ancestor — that's its stale base.
        - Switch back to ``pr-branch`` for the test to add the PR's
            commits on top.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q", "-b", "main")
        bp = repo / "BLUEPRINT.md"
        bp.write_text(ancestor, encoding="utf-8")
        _run_git(repo, "add", "BLUEPRINT.md")
        _run_git(repo, "commit", "-q", "-m", "ancestor")
        # Snapshot the branch-point now.
        _run_git(repo, "branch", "pr-branch")
        # The base-ref starts pointing at the ancestor too.
        _run_git(repo, "branch", "-f", "base-ref", "main")

        if base_after_concurrent_merge is not None:
            # Advance main with the "PR A merged" content.
            bp.write_text(base_after_concurrent_merge, encoding="utf-8")
            _run_git(repo, "add", "BLUEPRINT.md")
            _run_git(repo, "commit", "-q", "-m", "concurrently-merged-pr-a")
            _run_git(repo, "branch", "-f", "base-ref", "main")

        _run_git(repo, "checkout", "-q", "pr-branch")
        return repo

    def test_red_collision_with_concurrently_merged_pr(self, tmp_path: Path) -> None:
        # Ancestor (merge-base) has 1..3. While PR was in flight, main
        # advanced to 1..4 ("Alpha"). PR also picked 4 ("Beta") against
        # the stale base — that's the collision the gate must catch.
        repo = self._make_repo_with_branch_point(
            tmp_path,
            ancestor=_BASE_THREE,
            base_after_concurrent_merge=_BASE_AFTER_PR_A_MERGED,
        )
        bp = repo / "BLUEPRINT.md"
        bp.write_text(_PR_B_AGAINST_STALE_BASE, encoding="utf-8")
        _run_git(repo, "add", "BLUEPRINT.md")
        _run_git(repo, "commit", "-q", "-m", "pr-b")

        exit_code = ci_main(repo=repo, base_ref="base-ref")
        assert exit_code == 1

    def test_green_when_pr_picks_a_strictly_new_number(self, tmp_path: Path) -> None:
        # Post-rebase scenario: PR branched from main AFTER PR A
        # merged, so the merge-base IS _BASE_AFTER_PR_A_MERGED (1..4)
        # and the PR cleanly adds 5.
        repo = self._make_repo_with_branch_point(
            tmp_path,
            ancestor=_BASE_AFTER_PR_A_MERGED,
        )
        bp = repo / "BLUEPRINT.md"
        bp.write_text(_PR_C_NEW_NUMBER, encoding="utf-8")
        _run_git(repo, "add", "BLUEPRINT.md")
        _run_git(repo, "commit", "-q", "-m", "pr-c")

        exit_code = ci_main(repo=repo, base_ref="base-ref")
        assert exit_code == 0

    def test_skip_when_blueprint_unchanged_in_pr(self, tmp_path: Path) -> None:
        repo = self._make_repo_with_branch_point(tmp_path, ancestor=_BASE_THREE)
        # PR touches a different file — BLUEPRINT.md unchanged from base.
        (repo / "OTHER.md").write_text("hello", encoding="utf-8")
        _run_git(repo, "add", "OTHER.md")
        _run_git(repo, "commit", "-q", "-m", "pr-other")

        exit_code = ci_main(repo=repo, base_ref="base-ref")
        assert exit_code == 0

    def test_green_on_clean_addition_against_unchanged_base(self, tmp_path: Path) -> None:
        # Base has 1..3, PR adds 4 — the common case (no concurrent merge).
        repo = self._make_repo_with_branch_point(tmp_path, ancestor=_BASE_THREE)
        bp = repo / "BLUEPRINT.md"
        bp.write_text(
            _BASE_THREE.replace("### 17.2 Next", "4. **Four.** new\n\n### 17.2 Next"),
            encoding="utf-8",
        )
        _run_git(repo, "add", "BLUEPRINT.md")
        _run_git(repo, "commit", "-q", "-m", "pr-add-four")

        exit_code = ci_main(repo=repo, base_ref="base-ref")
        assert exit_code == 0

    def test_red_when_pr_introduces_local_gap(self, tmp_path: Path) -> None:
        # Even without a base collision, a 1..N gap on the PR side is
        # still wrong — ci_main must defer to the tree-local check too.
        repo = self._make_repo_with_branch_point(tmp_path, ancestor=_BASE_THREE)
        bp = repo / "BLUEPRINT.md"
        bp.write_text(
            _BASE_THREE.replace("### 17.2 Next", "5. **Five.** gap!\n\n### 17.2 Next"),
            encoding="utf-8",
        )
        _run_git(repo, "add", "BLUEPRINT.md")
        _run_git(repo, "commit", "-q", "-m", "pr-gap")

        exit_code = ci_main(repo=repo, base_ref="base-ref")
        assert exit_code == 1


class TestCiMergeBaseFailClosed:
    """Fix #8: BLUEPRINT touched but the merge-base §17.1 snapshot is unobtainable → fail CLOSED.

    ``merge_base_numbers`` is None when either ``git merge-base`` returns nothing
    OR BLUEPRINT.md did not exist at the merge-base (introduced since the
    branch-point). The old ci_main delegated that to the approximate
    common-prefix fallback in check_numbering_against_base, which returns ok=True
    for the canonical collision (both sides show [1..N]) — a silent pass of the
    exact cross-PR collision the gate exists to catch. ci_main now hard-fails
    (exit 1) in that case.
    """

    def test_blueprint_introduced_since_branch_point_fails_closed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Merge-base exists, but BLUEPRINT.md was introduced AFTER it: it is not
        # present at the merge-base commit, yet it IS in the PR diff and on the
        # base. base=[1..4] (concurrent PR A) and pr=[1..4] (stale) — the exact
        # collision. With merge_base_numbers=None the old code approx-passed; now
        # it reds.
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q", "-b", "main")
        # Branch-point commit has NO BLUEPRINT.md.
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "branch-point (no blueprint)")
        _run_git(repo, "branch", "pr-branch")
        _run_git(repo, "branch", "-f", "base-ref", "main")
        # base-ref advances: PR A introduced BLUEPRINT.md with [1..4].
        (repo / "BLUEPRINT.md").write_text(_BASE_AFTER_PR_A_MERGED, encoding="utf-8")
        _run_git(repo, "add", "BLUEPRINT.md")
        _run_git(repo, "commit", "-q", "-m", "pr-a introduced blueprint")
        _run_git(repo, "branch", "-f", "base-ref", "main")
        # PR branch (off the branch-point) also introduces BLUEPRINT.md with
        # [1..4] from the stale base — the collision.
        _run_git(repo, "checkout", "-q", "pr-branch")
        (repo / "BLUEPRINT.md").write_text(_PR_B_AGAINST_STALE_BASE, encoding="utf-8")
        _run_git(repo, "add", "BLUEPRINT.md")
        _run_git(repo, "commit", "-q", "-m", "pr-b introduced blueprint")

        from scripts.hooks.check_blueprint_invariant_numbering import _git_show, _merge_base  # noqa: PLC0415

        mb = _merge_base(repo, "base-ref")
        assert mb is not None  # merge-base resolves...
        assert _git_show(repo, mb, "BLUEPRINT.md") is None  # ...but BLUEPRINT isn't there.
        assert ci_main(repo=repo, base_ref="base-ref") == 1
        assert "merge-base" in capsys.readouterr().out.lower()
