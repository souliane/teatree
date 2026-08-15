"""The per-tick read-back index — same verdicts as the linear scan, O(N+M) not O(N*M) (#4466).

Intake re-ran :func:`existing_work_for_issue` over EVERY PR for EVERY candidate, so the cost
was the product of the two growing sets: measured at 13.52s for 143 candidates against 1000
merged PRs, most of a 60s scan budget spent re-deciding issues already decided. The index
bucketises each PR once by the integer tokens it carries, so a candidate reads only the PRs
that could possibly cite it.

The parity class below is the load-bearing one: the linear scan stays in the tree as the
oracle, and the index is only correct while it agrees with it on every signal.
"""

from teatree.loop.scanners.forge_readback import build_readback_index, existing_work_for_issue
from teatree.types import RawAPIDict

ISSUE = "https://github.com/souliane/teatree/issues/42"
GITLAB_ISSUE = "https://gitlab.com/acme/app/-/issues/42"


def _github_pr(*, url: str, head: str = "", body: str = "", title: str = "") -> RawAPIDict:
    return {"html_url": url, "head": {"ref": head}, "body": body, "title": title}


class TestReadbackIndexParity:
    """Every signal the linear scan finds, the index finds — with the same reason and evidence."""

    def _assert_parity(
        self,
        *,
        issue_url: str,
        ticket_number: str,
        open_prs: list[RawAPIDict],
        merged_prs: list[RawAPIDict] | None = None,
    ) -> None:
        expected = existing_work_for_issue(
            issue_url=issue_url,
            ticket_number=ticket_number,
            open_prs=open_prs,
            merged_prs=merged_prs,
        )
        actual = build_readback_index(open_prs, merged_prs or []).hit_for(
            issue_url=issue_url,
            ticket_number=ticket_number,
        )
        assert actual == expected

    def test_head_branch(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", head="42-feature")],
        )

    def test_body_url_ref(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", body=f"see {ISSUE}")],
        )

    def test_closes_keyword(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", body="Closes #42")],
        )

    def test_cited_ref_in_title(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", title="fix thing (#42)")],
        )

    def test_foreign_repo_never_binds(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/other/repo/pull/9", head="42-feature")],
        )

    def test_clean_when_nothing_cites_the_issue(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", head="7-other", body="Closes #7")],
        )

    def test_open_pr_wins_over_merged(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", head="42-open")],
            merged_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/8", head="42-merged")],
        )

    def test_merged_pr_when_no_open_pr_cites_it(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[],
            merged_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/8", head="42-merged")],
        )

    def test_first_matching_pr_supplies_the_evidence(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[
                _github_pr(url="https://github.com/souliane/teatree/pull/1", head="42-first"),
                _github_pr(url="https://github.com/souliane/teatree/pull/2", head="42-second"),
            ],
        )

    def test_signal_precedence_within_one_pr(self) -> None:
        self._assert_parity(
            issue_url=ISSUE,
            ticket_number="42",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", head="42-x", body="Closes #42")],
        )

    def test_gitlab_payload_shape(self) -> None:
        merge_request: RawAPIDict = {
            "web_url": "https://gitlab.com/acme/app/-/merge_requests/9",
            "source_branch": "42-feature",
            "description": "",
        }
        self._assert_parity(issue_url=GITLAB_ISSUE, ticket_number="42", open_prs=[merge_request])

    def test_unnumbered_issue_is_never_matched(self) -> None:
        self._assert_parity(
            issue_url="https://github.com/souliane/teatree/issues/0",
            ticket_number="",
            open_prs=[_github_pr(url="https://github.com/souliane/teatree/pull/9", head="main")],
        )


class TestReadbackIndexBoundary:
    """A longer number that merely CONTAINS the ticket number is a different issue."""

    def test_body_url_of_a_longer_number_does_not_bind(self) -> None:
        prs = [
            _github_pr(
                url="https://github.com/souliane/teatree/pull/9",
                body="see https://github.com/souliane/teatree/issues/4200",
            )
        ]
        assert build_readback_index(prs, []).hit_for(issue_url=ISSUE.replace("42", "420"), ticket_number="420") is None

    def test_linear_scan_agrees_the_longer_number_does_not_bind(self) -> None:
        prs = [
            _github_pr(
                url="https://github.com/souliane/teatree/pull/9",
                body="see https://github.com/souliane/teatree/issues/4200",
            )
        ]
        hit = existing_work_for_issue(issue_url=ISSUE.replace("42", "420"), ticket_number="420", open_prs=prs)
        assert hit is None


class TestReadbackIndexCost:
    """The defect itself: per-candidate cost must not scale with the PR corpus."""

    def test_each_pr_is_parsed_once_per_pass_not_once_per_candidate(self) -> None:
        prs = [
            _github_pr(url=f"https://github.com/souliane/teatree/pull/{n}", head=f"{n}-branch", body=f"Closes #{n}")
            for n in range(1, 301)
        ]
        index = build_readback_index([], prs)
        inspected = 0
        for number in range(5000, 5200):
            inspected += index.candidates_for(str(number))
        assert inspected == 0

    def test_a_candidate_only_reads_the_prs_that_cite_its_number(self) -> None:
        prs = [
            _github_pr(url=f"https://github.com/souliane/teatree/pull/{n}", head=f"{n}-branch") for n in range(1, 301)
        ]
        index = build_readback_index([], prs)
        assert index.candidates_for("150") == 1
