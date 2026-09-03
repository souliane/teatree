"""Tests for the intake landscape survey (:mod:`teatree.core.intake.landscape`, #2541).

Worktree gather runs against a real git repo built with the shared
``make_git_repo`` fixture (default-branch-independent); forge gather and the
issue classifier run against a fake ``CodeHostBackend`` so the deterministic
classification floor is pinned without a network. Each assertion fails if the
gather/classify behaviour regresses — no vacuous "object exists" checks.
"""

from pathlib import Path

from teatree.core.intake.landscape import (
    IssueDisposition,
    LandscapeSurvey,
    OpenPullRequest,
    RecommendedAction,
    classify_issue,
    survey_landscape,
    survey_open_prs,
    survey_worktrees,
)
from teatree.types import RawAPIDict
from tests._git_repo import make_git_repo, run_git

_APP = "https://github.com/acme/app"
_DOCS = "https://github.com/acme/docs"


class _FakeCodeHost:
    """Minimal ``CodeHostBackend`` stand-in for PR listing.

    ``my_prs`` is the payload ``list_my_prs`` returns; ``raise_on_list`` makes
    the call blow up so the warning-degradation path is exercised.
    """

    def __init__(self, *, my_prs: list[RawAPIDict] | None = None, raise_on_list: bool = False) -> None:
        self._my_prs = my_prs or []
        self._raise = raise_on_list

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        if self._raise:
            msg = "forge unavailable"
            raise RuntimeError(msg)
        return self._my_prs


class TestSurveyWorktrees:
    def test_clean_pushed_worktree_is_not_in_flight(self, tmp_path: Path) -> None:
        origin = make_git_repo(tmp_path / "origin", bare=True)
        clone = tmp_path / "clone"
        run_git(tmp_path, "clone", str(origin), str(clone))
        run_git(clone, "config", "user.email", "t@example.com")
        run_git(clone, "config", "user.name", "T")
        (clone / "f.txt").write_text("x", encoding="utf-8")
        run_git(clone, "add", ".")
        run_git(clone, "commit", "-m", "seed")
        run_git(clone, "push", "origin", "HEAD")

        states = survey_worktrees([clone])

        assert len(states) == 1
        assert states[0].has_uncommitted is False
        assert states[0].has_unpushed is False
        assert states[0].in_flight is False

    def test_dirty_worktree_is_flagged_uncommitted(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        (repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")

        states = survey_worktrees([repo])

        assert states[0].has_uncommitted is True
        assert states[0].in_flight is True

    def test_unpushed_commit_is_flagged(self, tmp_path: Path) -> None:
        origin = make_git_repo(tmp_path / "origin", bare=True)
        clone = tmp_path / "clone"
        run_git(tmp_path, "clone", str(origin), str(clone))
        run_git(clone, "config", "user.email", "t@example.com")
        run_git(clone, "config", "user.name", "T")
        (clone / "local-only.txt").write_text("y", encoding="utf-8")
        run_git(clone, "add", ".")
        run_git(clone, "commit", "-m", "local only, never pushed")

        states = survey_worktrees([clone])

        assert states[0].has_unpushed is True
        assert states[0].in_flight is True

    def test_missing_path_is_skipped(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        ghost = tmp_path / "does-not-exist"

        states = survey_worktrees([repo, ghost])

        assert [s.path for s in states] == [repo]


class TestSurveyOpenPrs:
    def test_gathers_url_and_referenced_issues(self) -> None:
        host = _FakeCodeHost(my_prs=[{"url": f"{_APP}/pull/7", "title": "Fix login (#41)", "body": "also helps #42"}])

        prs, warnings = survey_open_prs(host, author="me")

        assert warnings == []
        assert prs[0].url == f"{_APP}/pull/7"
        assert prs[0].referenced_issues == frozenset({"acme/app#41", "acme/app#42"})

    def test_cross_repo_reference_keeps_the_repo_it_names(self) -> None:
        host = _FakeCodeHost(my_prs=[{"url": f"{_APP}/pull/7", "title": "Fix login", "body": "closes acme/docs#41"}])

        prs, _ = survey_open_prs(host, author="me")

        assert prs[0].referenced_issues == frozenset({"acme/docs#41"})

    def test_unresolvable_pr_url_drops_the_reference(self) -> None:
        host = _FakeCodeHost(my_prs=[{"url": "", "title": "Fix login (#41)"}])

        prs, _ = survey_open_prs(host, author="me")

        assert prs[0].referenced_issues == frozenset()

    def test_forge_failure_degrades_to_warning_not_crash(self) -> None:
        host = _FakeCodeHost(raise_on_list=True)

        prs, warnings = survey_open_prs(host, author="me")

        assert prs == []
        assert len(warnings) == 1
        assert "could not list open PRs" in warnings[0]


class TestClassifyIssue:
    def _issue(self, number: int, title: str = "An issue", repo: str = _APP) -> RawAPIDict:
        return {"url": f"{repo}/issues/{number}", "title": title}

    def test_merged_pr_marks_issue_done_and_close(self) -> None:
        rec = classify_issue(
            self._issue(41),
            open_prs=[],
            merged_pr_issue_keys=frozenset({"acme/app#41"}),
        )

        assert rec.disposition is IssueDisposition.DONE
        assert rec.action is RecommendedAction.CLOSE
        assert "acme/app#41" in rec.evidence

    def test_merged_pr_in_another_repo_does_not_close_this_issue(self) -> None:
        rec = classify_issue(
            self._issue(41, repo=_DOCS),
            open_prs=[],
            merged_pr_issue_keys=frozenset({"acme/app#41"}),
        )

        assert rec.disposition is IssueDisposition.OPEN
        assert rec.action is RecommendedAction.KEEP

    def test_open_pr_marks_issue_partial_and_merge(self) -> None:
        pr = OpenPullRequest(url=f"{_APP}/pull/9", title="WIP (#50)", referenced_issues=frozenset({"acme/app#50"}))

        rec = classify_issue(self._issue(50), open_prs=[pr], merged_pr_issue_keys=frozenset())

        assert rec.disposition is IssueDisposition.PARTIAL
        assert rec.action is RecommendedAction.MERGE
        assert f"{_APP}/pull/9" in rec.evidence

    def test_open_pr_in_another_repo_does_not_claim_this_issue(self) -> None:
        pr = OpenPullRequest(url=f"{_APP}/pull/9", title="WIP (#50)", referenced_issues=frozenset({"acme/app#50"}))

        rec = classify_issue(self._issue(50, repo=_DOCS), open_prs=[pr], merged_pr_issue_keys=frozenset())

        assert rec.disposition is IssueDisposition.OPEN
        assert rec.action is RecommendedAction.KEEP

    def test_no_in_flight_signal_keeps_issue_open(self) -> None:
        rec = classify_issue(self._issue(99), open_prs=[], merged_pr_issue_keys=frozenset())

        assert rec.disposition is IssueDisposition.OPEN
        assert rec.action is RecommendedAction.KEEP

    def test_merged_takes_precedence_over_open_pr(self) -> None:
        pr = OpenPullRequest(url=f"{_APP}/pull/9", title="late (#50)", referenced_issues=frozenset({"acme/app#50"}))

        rec = classify_issue(
            self._issue(50),
            open_prs=[pr],
            merged_pr_issue_keys=frozenset({"acme/app#50"}),
        )

        assert rec.disposition is IssueDisposition.DONE
        assert rec.action is RecommendedAction.CLOSE


class TestSurveyLandscape:
    def test_assembles_worktrees_prs_and_recommendations(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        (repo / "dirty.txt").write_text("x", encoding="utf-8")
        host = _FakeCodeHost(my_prs=[{"url": f"{_APP}/pull/1", "title": "WIP (#50)"}])
        open_issues: list[RawAPIDict] = [
            {"url": f"{_APP}/issues/41", "title": "shipped"},
            {"url": f"{_APP}/issues/50", "title": "in flight"},
            {"url": f"{_APP}/issues/99", "title": "genuine"},
            {"url": f"{_DOCS}/issues/50", "title": "same number, other repo"},
        ]

        survey = survey_landscape(
            host=host,
            author="me",
            worktree_paths=[repo],
            open_issues=open_issues,
            merged_pr_issue_keys=frozenset({"acme/app#41"}),
        )

        assert isinstance(survey, LandscapeSurvey)
        assert survey.in_flight_worktrees[0].path == repo
        assert survey.open_prs[0].url == f"{_APP}/pull/1"
        dispositions = {r.issue_url: r.disposition for r in survey.recommendations}
        assert dispositions[f"{_APP}/issues/41"] is IssueDisposition.DONE
        assert dispositions[f"{_APP}/issues/50"] is IssueDisposition.PARTIAL
        assert dispositions[f"{_APP}/issues/99"] is IssueDisposition.OPEN
        assert dispositions[f"{_DOCS}/issues/50"] is IssueDisposition.OPEN
        # Two actionable (close + merge); the genuine-open one is not actionable.
        assert {r.action for r in survey.actionable} == {RecommendedAction.CLOSE, RecommendedAction.MERGE}

    def test_forge_outage_is_reported_in_warnings_not_fatal(self, tmp_path: Path) -> None:
        repo = make_git_repo(tmp_path / "repo")
        host = _FakeCodeHost(raise_on_list=True)

        survey = survey_landscape(
            host=host,
            author="me",
            worktree_paths=[repo],
            open_issues=[],
            merged_pr_issue_keys=frozenset(),
        )

        assert survey.open_prs == []
        assert any("could not list open PRs" in w for w in survey.warnings)
        # Local landscape still gathered despite the forge outage.
        assert len(survey.worktrees) == 1
