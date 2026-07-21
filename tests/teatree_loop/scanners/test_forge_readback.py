"""Pre-dispatch forge read-back tests (fleet-safety Stage 1).

The read-back is a NET: it skips an issue whose ``<ticket_number>-*`` branch or a
referencing PR already exists on the forge, closing most of the cross-instance
double-claim window the local claim ledger cannot see. It must match only within
the issue's own repo and must never let a forge error block the claim path.
"""

import logging
from dataclasses import dataclass, field

from teatree.loop.scanners.forge_readback import existing_work_for_issue, fetch_merged_prs, fetch_open_prs, issue_number
from teatree.types import RawAPIDict

ISSUE = "https://github.com/souliane/teatree/issues/42"
GITLAB_ISSUE = "https://gitlab.com/acme/app/-/issues/42"


def _github_pr(*, url: str, head: str = "", body: str = "", title: str = "") -> RawAPIDict:
    return {"html_url": url, "head": {"ref": head}, "body": body, "title": title}


def _gitlab_mr(*, url: str, source_branch: str = "", body: str = "") -> RawAPIDict:
    return {"web_url": url, "source_branch": source_branch, "description": body}


class TestIssueNumber:
    def test_extracts_trailing_number(self) -> None:
        assert issue_number(ISSUE) == "42"

    def test_zero_is_not_a_number(self) -> None:
        assert issue_number("https://github.com/o/r/issues/0") == ""

    def test_no_trailing_number(self) -> None:
        assert issue_number("https://github.com/o/r/issues/42/") == ""


class TestExistingWorkForIssue:
    def test_hits_on_deterministic_head_branch(self) -> None:
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="42-feature-add-thing")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_head_branch"
        assert hit.evidence_url == "https://github.com/souliane/teatree/pull/9"

    def test_hits_on_issue_url_in_body(self) -> None:
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="unrelated", body=f"see {ISSUE}")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_body_ref"

    def test_hits_on_closes_keyword(self) -> None:
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="x", body="Closes #42")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_closes_ref"

    def test_hits_on_non_numeric_prefixed_branch(self) -> None:
        # A branch like ``impl-42-presets`` cites the issue number as a whole word
        # even though it is not the deterministic ``42``/``42-*`` worktree branch.
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="impl-42-presets")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_head_branch"

    def test_hits_on_hash_ref_in_title(self) -> None:
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="wip", title="Fix presets (#42)")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_cited_ref"

    def test_hits_on_hash_ref_in_body(self) -> None:
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="wip", body="towards #42 groundwork")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_cited_ref"

    def test_hash_ref_requires_whole_number(self) -> None:
        # ``#420`` must not bind issue 42.
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="wip", body="see #420")]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs) is None

    def test_merged_pr_closes_ref_is_a_hit(self) -> None:
        # A fully-implemented issue whose PRs are MERGED must not be re-claimed.
        merged = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="x", body="Closes #42")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=[], merged_prs=merged)
        assert hit is not None
        assert hit.reason == "merged_pr_closes_ref"

    def test_merged_pr_branch_is_a_hit(self) -> None:
        merged = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="impl-42-presets")]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=[], merged_prs=merged)
        assert hit is not None
        assert hit.reason == "merged_pr_head_branch"

    def test_merged_pr_in_other_repo_does_not_match(self) -> None:
        merged = [_github_pr(url="https://github.com/souliane/other/pull/9", head="x", body="Closes #42")]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=[], merged_prs=merged) is None

    def test_clean_returns_none(self) -> None:
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="99-other", body="unrelated")]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs) is None

    def test_unrelated_open_pr_is_not_a_false_hit(self) -> None:
        # A same-repo PR with no branch/body/title reference to issue 42 is clean,
        # even when it cites a different issue number.
        prs = [_github_pr(url="https://github.com/souliane/teatree/pull/9", head="impl-presets", body="see #420")]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs) is None

    def test_other_repo_branch_does_not_match(self) -> None:
        # A `42-*` branch in a DIFFERENT repo must not bind this issue.
        prs = [_github_pr(url="https://github.com/souliane/other/pull/9", head="42-feature-add-thing")]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs) is None

    def test_other_repo_closes_ref_does_not_match(self) -> None:
        prs = [_github_pr(url="https://github.com/souliane/other/pull/9", head="x", body="Closes #42")]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs) is None

    def test_no_ticket_number_returns_none(self) -> None:
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="", open_prs=[]) is None

    def test_unparseable_issue_url_fails_open_not_closed(self) -> None:
        # An issue URL with no parseable repo slug cannot be scoped to a repo, so a
        # foreign-repo PR whose branch matches the ticket number must NOT skip the
        # issue (that would strand it). Fail open — the caller claims.
        unparsable = "https://internal.example/tracker/42"
        prs = [_github_pr(url="https://github.com/souliane/other/pull/9", head="42-feature")]
        assert existing_work_for_issue(issue_url=unparsable, ticket_number="42", open_prs=prs) is None

    def test_matches_gitlab_source_branch(self) -> None:
        prs = [_gitlab_mr(url="https://gitlab.com/acme/app/-/merge_requests/9", source_branch="42-feature")]
        hit = existing_work_for_issue(issue_url=GITLAB_ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_head_branch"

    def test_matches_gitlab_description_ref(self) -> None:
        prs = [_gitlab_mr(url="https://gitlab.com/acme/app/-/merge_requests/9", body=f"resolves {GITLAB_ISSUE}")]
        hit = existing_work_for_issue(issue_url=GITLAB_ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_body_ref"

    def test_matches_alternate_head_ref_key(self) -> None:
        prs = [{"html_url": "https://github.com/souliane/teatree/pull/9", "head_ref": "42-feature"}]
        hit = existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs)
        assert hit is not None
        assert hit.reason == "open_pr_head_branch"

    def test_urlless_pr_is_ignored(self) -> None:
        # No URL → no repo to scope against → skipped, never a spurious hit.
        prs: list[RawAPIDict] = [{"head": {"ref": "42-feature"}}]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs) is None

    def test_same_repo_pr_with_no_branch_or_body_is_clean(self) -> None:
        # A same-repo PR carrying an empty head dict and no body must not spuriously match.
        prs: list[RawAPIDict] = [{"html_url": "https://github.com/souliane/teatree/pull/9", "head": {}}]
        assert existing_work_for_issue(issue_url=ISSUE, ticket_number="42", open_prs=prs) is None


@dataclass
class _Host:
    prs: list[RawAPIDict] = field(default_factory=list)
    merged: list[RawAPIDict] = field(default_factory=list)
    raises: bool = False
    seen_authors: list[str] = field(default_factory=list)
    seen_merged_authors: list[str] = field(default_factory=list)

    def list_my_prs(self, *, author: str) -> list[RawAPIDict]:
        self.seen_authors.append(author)
        if self.raises:
            msg = "forge down"
            raise RuntimeError(msg)
        return self.prs

    def list_my_merged_prs(self, *, author: str) -> list[RawAPIDict]:
        self.seen_merged_authors.append(author)
        if self.raises:
            msg = "forge down"
            raise RuntimeError(msg)
        return self.merged


class TestFetchOpenPrs:
    def test_unions_and_dedupes_by_url(self) -> None:
        host = _Host(prs=[_github_pr(url="https://github.com/o/r/pull/1")])
        prs = fetch_open_prs(host, authors=("alice", "alice-bot"))
        assert len(prs) == 1
        assert host.seen_authors == ["alice", "alice-bot"]

    def test_forge_error_degrades_to_empty_never_raises(self) -> None:
        host = _Host(raises=True)
        assert fetch_open_prs(host, authors=("alice",)) == []

    def test_forge_error_logs_warning_naming_author(self, caplog) -> None:
        # F5.10: a degraded read-back NET is surfaced at warning (not debug) so a
        # systematically failing author/token is visible, and the author is named.
        host = _Host(raises=True)
        with caplog.at_level(logging.WARNING, logger="teatree.loop.scanners.forge_readback"):
            assert fetch_open_prs(host, authors=("alice",)) == []
        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert warnings
        assert any("alice" in rec.getMessage() for rec in warnings)

    def test_urlless_pr_is_kept_without_dedup(self) -> None:
        host = _Host(prs=[{"head": {"ref": "x"}}])
        prs = fetch_open_prs(host, authors=("alice",))
        assert len(prs) == 1


class TestFetchMergedPrs:
    def test_unions_and_dedupes_by_url(self) -> None:
        host = _Host(merged=[_github_pr(url="https://github.com/o/r/pull/1")])
        prs = fetch_merged_prs(host, authors=("alice", "alice-bot"))
        assert len(prs) == 1
        assert host.seen_merged_authors == ["alice", "alice-bot"]

    def test_forge_error_degrades_to_empty_never_raises(self) -> None:
        host = _Host(raises=True)
        assert fetch_merged_prs(host, authors=("alice",)) == []
