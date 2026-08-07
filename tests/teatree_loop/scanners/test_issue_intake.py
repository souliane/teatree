"""Behaviour tests for the ONE unified issue-intake scanner (#3634).

The scanner is the discovery + claim half of the factory's intake. Two scoped
discovery queries (per trusted author, and per admit label) feed one top-down
decision function; claims go through the TOCTOU-safe
:meth:`ImplementedIssueMarker.claim` so a re-tick — or a concurrent overlay —
never double-dispatches the same issue.
"""

import datetime as dt
from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase

from teatree.core.intake.factory_admission import resolve_umbrella_labels
from teatree.core.models import (
    ImplementedIssueMarker,
    Ticket,
    TrustedIdentity,
    UnclaimedIntakeCandidate,
    WaitingCandidate,
)
from teatree.loop.scanners.issue_intake import (
    IssueIntakeScanner,
    author_is_trusted,
    issue_author,
    issue_created_at,
    issue_url,
)
from teatree.types import RawAPIDict

OWNER = "souliane"
COLLEAGUE = "trusted-colleague"
STRANGER = "random-user"
TRUSTED = (OWNER, COLLEAGUE)


@dataclass
class _Host:
    """Minimal CodeHostBackend stub — only the methods the scanner calls.

    ``authored`` is keyed by author handle: the scanner's candidate query is
    author-scoped, so the stub answers per-author exactly like the forge does.
    """

    user: str = OWNER
    authored: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    #: Keyed by admit label — the owner-admission discovery query.
    labeled: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    open_prs: list[RawAPIDict] = field(default_factory=list)
    merged_prs: list[RawAPIDict] = field(default_factory=list)
    #: Every author handle the scanner asked the forge about — the intake surface.
    queried_authors: list[str] = field(default_factory=list)
    #: Every admit label the scanner asked the forge about.
    queried_labels: list[str] = field(default_factory=list)
    #: The ``repo_slugs`` passed with each query — the repo-scope surface.
    queried_repo_slugs: list[tuple[str, ...]] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_authored_issues(self, *, author: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        self.queried_authors.append(author)
        self.queried_repo_slugs.append(repo_slugs)
        issues = list(self.authored.get(author, []))
        # Model the forge's ``repo:`` qualifier: a scoped query returns only issues
        # whose repo slug is in the requested set (an unscoped query returns all).
        if repo_slugs:
            issues = [issue for issue in issues if _issue_repo_slug(issue) in repo_slugs]
        return issues

    def list_labeled_issues(self, *, label: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        self.queried_labels.append(label)
        self.queried_repo_slugs.append(repo_slugs)
        issues = list(self.labeled.get(label, []))
        if repo_slugs:
            issues = [issue for issue in issues if _issue_repo_slug(issue) in repo_slugs]
        return issues

    def list_my_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return self.open_prs

    def list_my_merged_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return self.merged_prs


def _issue(
    url: str,
    *,
    author: str,
    labels: list[str] | None = None,
    state: str = "open",
    created_at: str = "",
    **extra: object,
) -> RawAPIDict:
    """A GitHub-shaped issue payload (``user.login`` is the author).

    *extra* overrides any payload field (``title``, ``body``, …) so a test that cares
    about one of them says so without growing this signature.
    """
    issue: RawAPIDict = {
        "web_url": url,
        "title": "do it",
        "labels": labels or [],
        "state": state,
        "user": {"login": author},
        "body": "",
        **extra,
    }
    if created_at:
        issue["created_at"] = created_at
    return issue


def _issue_repo_slug(issue: RawAPIDict) -> str:
    """``owner/repo`` parsed from a GitHub issue web URL (``.../owner/repo/issues/N``)."""
    parts = str(issue.get("web_url", "")).split("/")
    return "/".join(parts[3:5]) if len(parts) >= 6 else ""


class _PublicRepoTestCase(TestCase):
    """Every issue below lives on a PUBLIC repo — the strict, author-gated path.

    ``repo_is_internal`` is the visibility half of the shared classifier; pinning
    it False keeps the tests off the live ``gh``/``glab`` probe and on the branch
    that actually enforces author trust.
    """

    OVERLAY = "acme"
    LABEL = "auto-implement"
    URL_A = "https://github.com/souliane/teatree/issues/100"
    URL_B = "https://github.com/souliane/teatree/issues/101"

    def setUp(self) -> None:
        patcher = patch("teatree.core.review.author_trust.repo_is_internal", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _scanner(self, host: _Host, **overrides: object) -> IssueIntakeScanner:
        kwargs: dict[str, object] = {
            "host": host,
            "admit_label": self.LABEL,
            "overlay_name": self.OVERLAY,
            # Resolved, never a literal: the shipped marker set lives in defaults.toml
            # alone, so these tests exercise what the loop actually builds.
            "umbrella_labels": resolve_umbrella_labels(self.OVERLAY),
            "trusted_authors": TRUSTED,
            "identities": (OWNER,),
        }
        kwargs.update(overrides)
        return IssueIntakeScanner(**kwargs)


class IssueIntakeUmbrellaTests(_PublicRepoTestCase):
    """An umbrella/epic row is never claimed — a bounded slot for an unbounded scope (#4105)."""

    #: souliane/teatree#4048's real shape: a members checklist, no acceptance criteria.
    EPIC_BODY = "## Why these are one thing\n\nHarness-owned red.\n\n## Members\n\n- [x] #3848\n- [x] #3892\n"
    BUG_BODY = "## Observed\n\nThe shard times out.\n\n## Acceptance\n\n- it does not.\n"

    def test_the_implementable_issue_is_claimed_and_the_epic_is_not(self) -> None:
        """The acceptance criterion: both filed by the owner, only the bounded one is claimed."""
        epic = _issue(self.URL_A, author=OWNER, labels=["epic"], title="Epic: CI-harness reliability")
        bug = _issue(self.URL_B, author=OWNER, body=self.BUG_BODY, title="shard times out")
        host = _Host(authored={OWNER: [epic, bug]})

        signals = self._scanner(host).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_B]
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_an_unlabelled_epic_is_declined_on_its_shape_alone(self) -> None:
        """The label convention is not load-bearing — the structural signal stands alone."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER, body=self.EPIC_BODY)]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_a_declined_epic_is_not_recorded_as_a_waiting_candidate(self) -> None:
        """Waiting means "admissible, no budget" — a declined row is neither.

        ``can_claim=False`` is what makes this falsifiable: on a tick that claims
        nothing, an ADMITTED candidate is recorded as waiting, so a gate that never
        fired would leave the epic sitting in the queue as a witness (#4238).
        """
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER, labels=["epic"])]})

        self._scanner(host, can_claim=False).scan()

        assert not UnclaimedIntakeCandidate.objects.filter(overlay=self.OVERLAY).exists()

    def test_the_admit_label_does_not_override_the_umbrella_decline(self) -> None:
        epic = _issue(self.URL_A, author=STRANGER, labels=["epic", self.LABEL])
        host = _Host(labeled={self.LABEL: [epic]})

        assert self._scanner(host).scan() == []

    def test_an_overlay_configured_marker_replaces_the_shipped_set(self) -> None:
        rollup = _issue(self.URL_A, author=OWNER, labels=["roll-up"])
        shipped = _issue(self.URL_B, author=OWNER, labels=["epic"])
        host = _Host(authored={OWNER: [rollup, shipped]})

        signals = self._scanner(host, umbrella_labels=frozenset({"roll-up"})).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_B]


class IssueIntakeAuthorTrustIntakeTests(_PublicRepoTestCase):
    """Intake is by TRUSTED AUTHOR — no label required (#3235)."""

    def test_owner_authored_unlabelled_issue_is_claimed_and_emitted(self) -> None:
        """The owner's own issue (a ``user_identity_aliases`` handle), with NO label, is claimed."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        signals = self._scanner(host).scan()

        assert [s.kind for s in signals] == ["issue_intake.admitted"]
        assert signals[0].payload["url"] == self.URL_A
        assert signals[0].payload["auto_start"] is True
        marker = ImplementedIssueMarker.objects.get(issue_url=self.URL_A, overlay=self.OVERLAY)
        assert marker.state == ImplementedIssueMarker.State.DISPATCHED

    def test_allowlisted_colleague_unlabelled_issue_is_claimed(self) -> None:
        """A ``trusted_issue_authors`` handle, with NO label, is claimed."""
        host = _Host(authored={COLLEAGUE: [_issue(self.URL_B, author=COLLEAGUE)]})

        signals = self._scanner(host).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_B]
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_B, overlay=self.OVERLAY).exists()

    def test_trusted_identity_row_alone_makes_an_author_trusted(self) -> None:
        """The third UNION source: a ``TrustedIdentity`` row, with no config entry at all."""
        TrustedIdentity.objects.create(platform=TrustedIdentity.Platform.GITHUB, handle="db-only-human")
        host = _Host(authored={"db-only-human": [_issue(self.URL_A, author="db-only-human")]})

        signals = self._scanner(host, trusted_authors=()).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_A]

    def test_author_match_is_case_insensitive(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author="Souliane")]})

        assert len(self._scanner(host).scan()) == 1

    def test_gitlab_shaped_author_payload_is_read(self) -> None:
        issue: RawAPIDict = {
            "web_url": self.URL_A,
            "title": "t",
            "labels": [],
            "state": "opened",
            "author": {"username": OWNER},
        }
        host = _Host(authored={OWNER: [issue]})

        assert len(self._scanner(host).scan()) == 1

    def test_forge_is_queried_for_every_trusted_author(self) -> None:
        host = _Host()

        self._scanner(host).scan()

        assert sorted(host.queried_authors) == sorted(TRUSTED)

    def test_stranger_is_never_queried(self) -> None:
        host = _Host()

        self._scanner(host).scan()

        assert STRANGER not in host.queried_authors

    def test_no_trusted_author_claims_nothing(self) -> None:
        """Fail-closed: an empty trusted set intakes nothing, even with issues on the forge."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        assert self._scanner(host, trusted_authors=()).scan() == []
        assert not ImplementedIssueMarker.objects.exists()


class IssueIntakeUntrustedAuthorRefusalTests(_PublicRepoTestCase):
    """FAIL-CLOSED. An issue authored outside the trusted set is NEVER auto-implemented.

    This is the safety keystone of #3235: intake without a human label means the
    issue author is the only thing standing between a stranger on a public repo and
    the autonomous factory. The gate is enforced per-issue at claim time — not merely
    by the author-scoped query — so an issue that surfaces by ANY other route (a
    forge query that over-returns, a poisoned payload, a future backend that widens
    the scope) is still refused: no signal, no marker, no dispatch.
    """

    def test_stranger_authored_issue_is_never_claimed(self) -> None:
        """THE fail-closed test: a `random-user` issue that surfaced anyway is refused outright."""
        # The forge hands back a stranger's issue under a TRUSTED author's query —
        # the exact "it somehow surfaced" case the per-issue gate exists to refuse.
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=STRANGER)]})

        signals = self._scanner(host).scan()

        assert signals == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()
        assert not ImplementedIssueMarker.objects.exists()

    def test_stranger_authored_issue_is_refused_on_a_private_repo(self) -> None:
        """The classifier's internal-repo bypass must NOT open intake to an unlisted author.

        :func:`classify_author` calls a PRIVATE repo's every author trusted (the user
        owns access control there) — correct for judging a MERGE, far too loose for
        handing an outsider the keys to the factory. Intake additionally REQUIRES
        explicit trusted-set membership, so the bypass cannot widen it.
        """
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=STRANGER)]})

        with patch("teatree.core.review.author_trust.repo_is_internal", return_value=True):
            assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_authorless_issue_is_refused(self) -> None:
        """An unresolvable author is an UNTRUSTED author — never a wildcard."""
        host = _Host(authored={OWNER: [{"web_url": self.URL_A, "title": "t", "labels": [], "state": "open"}]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_blank_author_is_refused(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author="   ")]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_unparseable_issue_url_is_refused(self) -> None:
        """No resolvable repo slug means no classifiable trust decision — refuse."""
        host = _Host(authored={OWNER: [_issue("https://example.invalid/not-an-issue", author=OWNER)]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_a_stranger_never_starves_a_trusted_sibling(self) -> None:
        host = _Host(
            authored={
                OWNER: [_issue(self.URL_A, author=STRANGER), _issue(self.URL_B, author=OWNER)],
            }
        )

        signals = self._scanner(host).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_B]
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()


class IssueIntakeNeedsTriageGateTests(_PublicRepoTestCase):
    """``needs-triage`` HOLDS a trusted-author issue — the maintainer override survives #3235.

    The maintainer applies ``needs-triage`` to withhold an issue from the autonomous
    factory until they have reviewed it. The scanner is the claim chokepoint, so the
    gate filters such issues out at selection time — never claimed, never dispatched,
    no marker row.
    """

    def test_needs_triage_holds_a_trusted_author_issue(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER, labels=["needs-triage"])]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_needs_triage_does_not_starve_a_clean_sibling(self) -> None:
        host = _Host(
            authored={
                OWNER: [
                    _issue(self.URL_A, author=OWNER, labels=["needs-triage"]),
                    _issue(self.URL_B, author=OWNER),
                ]
            }
        )

        signals = self._scanner(host).scan()

        assert {s.payload["url"] for s in signals} == {self.URL_B}
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_dict_shaped_needs_triage_label_is_honoured(self) -> None:
        issue = _issue(self.URL_A, author=OWNER)
        issue["labels"] = [{"name": "needs-triage"}]
        host = _Host(authored={OWNER: [issue]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()


class IssueIntakeAdmitLabelTests(_PublicRepoTestCase):
    """Rule 4: the owner-applied admit label is the ONLY route in for an untrusted author."""

    def test_labeled_stranger_issue_is_admitted(self) -> None:
        host = _Host(labeled={self.LABEL: [_issue(self.URL_A, author=STRANGER, labels=[self.LABEL])]})

        signals = self._scanner(host).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_A]
        assert signals[0].payload["verdict"] == "act_admitted"

    def test_label_scoped_query_fires_once_for_the_configured_label(self) -> None:
        host = _Host()

        self._scanner(host).scan()

        assert host.queried_labels == [self.LABEL]

    def test_no_label_query_when_no_admit_label_is_configured(self) -> None:
        host = _Host()

        self._scanner(host, admit_label="").scan()

        assert host.queried_labels == []

    def test_needs_triage_outranks_the_admit_label(self) -> None:
        issue = _issue(self.URL_A, author=STRANGER, labels=[self.LABEL, "needs-triage"])
        host = _Host(labeled={self.LABEL: [issue]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_trusted_author_is_admitted_with_no_label_at_all(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        signals = self._scanner(host, admit_label="").scan()

        assert [s.payload["verdict"] for s in signals] == ["act_trusted_author"]


class IssueIntakeClaimLifecycleTests(_PublicRepoTestCase):
    """Selection hygiene + claim idempotency, on the author-trust intake path."""

    def test_closed_issue_is_skipped(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER, state="closed")]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_missing_state_field_treated_as_open(self) -> None:
        issue = _issue(self.URL_A, author=OWNER)
        del issue["state"]
        host = _Host(authored={OWNER: [issue]})

        assert len(self._scanner(host).scan()) == 1

    def test_issue_without_url_is_skipped(self) -> None:
        host = _Host(authored={OWNER: [{"title": "no url", "labels": [], "user": {"login": OWNER}}]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_second_claim_of_same_issue_is_skipped(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        first = self._scanner(host).scan()
        second = self._scanner(host).scan()

        assert len(first) == 1
        assert second == []
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_A, overlay=self.OVERLAY).count() == 1

    def test_same_issue_under_two_trusted_authors_is_deduped_by_url(self) -> None:
        issue = _issue(self.URL_A, author=OWNER)
        host = _Host(authored={OWNER: [issue], COLLEAGUE: [issue]})

        signals = self._scanner(host).scan()

        assert len(signals) == 1
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).count() == 1


class IssueIntakeBudgetCapTests(_PublicRepoTestCase):
    """A single ``scan()`` claims at most ``max_concurrent - in_flight`` NEW issues.

    The factory only gates whether the scanner runs; without an in-loop cap the
    scan claims EVERY candidate in one tick, bursting the whole open backlog past
    the single-ticket budget. The cap stops the candidate loop the moment the
    live in-flight count reaches ``max_concurrent``.
    """

    def _n_owner_issues(self, count: int) -> _Host:
        issues = [_issue(f"https://github.com/souliane/teatree/issues/{200 + i}", author=OWNER) for i in range(count)]
        return _Host(authored={OWNER: issues})

    def test_max_concurrent_one_claims_a_single_issue(self) -> None:
        host = self._n_owner_issues(5)

        signals = self._scanner(host, max_concurrent=1).scan()

        assert len(signals) == 1
        assert ImplementedIssueMarker.objects.in_flight_count(self.OVERLAY) == 1

    def test_remaining_budget_accounts_for_already_in_flight(self) -> None:
        ImplementedIssueMarker.objects.claim("https://github.com/souliane/teatree/issues/1", overlay=self.OVERLAY)
        assert ImplementedIssueMarker.objects.in_flight_count(self.OVERLAY) == 1
        host = self._n_owner_issues(5)

        signals = self._scanner(host, max_concurrent=3).scan()

        assert len(signals) == 2
        assert ImplementedIssueMarker.objects.in_flight_count(self.OVERLAY) == 3

    def test_zero_cap_is_uncapped_and_claims_all_candidates(self) -> None:
        host = self._n_owner_issues(4)

        signals = self._scanner(host, max_concurrent=0).scan()

        assert len(signals) == 4


class IssueIntakeGovernorTests(_PublicRepoTestCase):
    """New intake also brakes when the headless-admission governor denies (F9)."""

    def test_governor_deny_brakes_new_intake(self) -> None:
        # A DENY reason from the admission governor stops the candidate loop even
        # when the static budget has room (the log names the reason).
        host = _Host(
            authored={OWNER: [_issue("https://github.com/souliane/teatree/issues/900", author=OWNER)]},
        )
        with patch(
            "teatree.core.headless_admission.headless_admission_denied_reason",
            return_value="congestion: headless pool saturated",
        ):
            signals = self._scanner(host, max_concurrent=0).scan()
        assert signals == []
        assert ImplementedIssueMarker.objects.in_flight_count(self.OVERLAY) == 0

    def test_governor_fail_open_leaves_intake_unchanged(self) -> None:
        # A None reason (fail-open) does not brake intake — the issue is claimed.
        host = _Host(
            authored={OWNER: [_issue("https://github.com/souliane/teatree/issues/901", author=OWNER)]},
        )
        with patch(
            "teatree.core.headless_admission.headless_admission_denied_reason",
            return_value=None,
        ):
            signals = self._scanner(host, max_concurrent=0).scan()
        assert len(signals) == 1


class IssueIntakeReadbackTests(_PublicRepoTestCase):
    """Pre-dispatch forge read-back: an already-PR'd trusted-author issue is NOT re-claimed.

    The local claim ledger cannot see another instance's work, so before claiming
    the scanner reads the forge for an existing ``<num>-*`` branch or a referencing
    open/merged PR and skips when found — closing most of the double-claim window.
    """

    def test_skips_claim_when_open_pr_branch_exists(self) -> None:
        host = _Host(
            authored={OWNER: [_issue(self.URL_A, author=OWNER)]},
            open_prs=[{"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "100-feature-x"}}],
        )

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_skips_claim_when_merged_pr_closes_issue(self) -> None:
        host = _Host(
            authored={OWNER: [_issue(self.URL_A, author=OWNER)]},
            merged_prs=[
                {"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "x"}, "body": "Closes #100"}
            ],
        )

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_skips_claim_for_an_allowlisted_colleagues_already_prd_issue(self) -> None:
        """The read-back guard is author-agnostic — a colleague's issue is guarded identically."""
        host = _Host(
            authored={COLLEAGUE: [_issue(self.URL_A, author=COLLEAGUE)]},
            open_prs=[{"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "100-feature-x"}}],
        )

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.URL_A).exists()

    def test_claims_when_forge_is_clean(self) -> None:
        host = _Host(
            authored={OWNER: [_issue(self.URL_A, author=OWNER)]},
            open_prs=[{"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "999-unrelated"}}],
        )

        signals = self._scanner(host).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_A]
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_A, overlay=self.OVERLAY).exists()

    def test_disabled_readback_claims_without_forge_query(self) -> None:
        host = _Host(
            authored={OWNER: [_issue(self.URL_A, author=OWNER)]},
            open_prs=[{"html_url": "https://github.com/souliane/teatree/pull/7", "head": {"ref": "100-feature-x"}}],
        )

        signals = self._scanner(host, readback_enabled=False).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_A]


class IssueIntakeRepoScopeTests(_PublicRepoTestCase):
    """Repo-scoped intake — app handles skipped, cross-repo issues refused (the firehose fix).

    Two failures the pre-fix scanner had: (1) it queried EVERY trusted handle,
    including the ``app/github-actions`` CI-bot row, whose ``author:`` search
    returns a 1000-result firehose of bot issues from all of GitHub; (2) it never
    scoped the query to the overlay's own repos, so a trusted human's issue filed
    on SOMEONE ELSE's public repo passed the author gate and got claimed — a
    cross-repo safety hole, not just noise.
    """

    OVERLAY_REPO = "souliane/teatree"
    FOREIGN_URL = "https://github.com/stranger/other-repo/issues/7"

    def test_app_handle_is_never_queried(self) -> None:
        """A ``/``-containing handle (app/github-actions) can't author issues — skipped, no wasted query."""
        TrustedIdentity.objects.create(platform=TrustedIdentity.Platform.GITHUB, handle="app/github-actions")
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        self._scanner(host, repo_slugs=(self.OVERLAY_REPO,)).scan()

        assert not any("/" in handle for handle in host.queried_authors)

    def test_repo_slugs_are_plumbed_into_every_query(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        self._scanner(host, repo_slugs=(self.OVERLAY_REPO,)).scan()

        assert host.queried_repo_slugs
        assert all(slugs == (self.OVERLAY_REPO,) for slugs in host.queried_repo_slugs)

    def test_trusted_author_issue_on_foreign_repo_is_not_claimed(self) -> None:
        """The cross-repo SAFETY pin: an owner issue on someone else's repo is never claimed."""
        host = _Host(authored={OWNER: [_issue(self.FOREIGN_URL, author=OWNER)]})

        signals = self._scanner(host, repo_slugs=(self.OVERLAY_REPO,)).scan()

        assert signals == []
        assert not ImplementedIssueMarker.objects.filter(issue_url=self.FOREIGN_URL).exists()

    def test_trusted_author_issue_on_own_repo_is_still_claimed(self) -> None:
        """Regression: an owner issue on the overlay's OWN repo is claimed as before."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        signals = self._scanner(host, repo_slugs=(self.OVERLAY_REPO,)).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_A]
        assert ImplementedIssueMarker.objects.filter(issue_url=self.URL_A, overlay=self.OVERLAY).exists()


def _assigned(issue: RawAPIDict, assignee: str) -> RawAPIDict:
    return {**issue, "assignees": [{"login": assignee}]}


class IssueIntakeExistingWorkTests(_PublicRepoTestCase):
    """Rule 2: a ticket already owning the URL blocks a second intake (#4133).

    Ownership is every state but IGNORED. ``_handle_orchestrator`` reuses the
    existing row (``get_or_create(issue_url=...)``) and returns early for anything
    past NOT_STARTED, so a re-admission cannot schedule work — it only claims an
    ``ImplementedIssueMarker``, holds an intake budget slot until the dead grace
    abandons it, and re-admits on the next tick, forever.
    """

    def _ticket_at(self, state: str) -> None:
        Ticket.objects.create(issue_url=self.URL_A, overlay=self.OVERLAY, role=Ticket.Role.AUTHOR, state=state)

    def test_active_ticket_for_the_url_is_ignored(self) -> None:
        self._ticket_at(Ticket.State.NOT_STARTED)
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_a_planned_ticket_blocks_re_admission(self) -> None:
        self._ticket_at(Ticket.State.PLANNED)
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_a_delivered_ticket_blocks_re_admission(self) -> None:
        self._ticket_at(Ticket.State.DELIVERED)
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        assert self._scanner(host).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_an_ignored_ticket_leaves_the_issue_re_claimable(self) -> None:
        # The release valve: IGNORED is how an abandoned or dead ticket hands its
        # issue back, so the fix above cannot wedge an issue permanently.
        self._ticket_at(Ticket.State.IGNORED)
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        assert len(self._scanner(host).scan()) == 1

    def test_every_state_but_ignored_blocks_re_admission(self) -> None:
        blocking = []
        for state in Ticket.State.values:
            Ticket.objects.all().delete()
            ImplementedIssueMarker.objects.all().delete()
            self._ticket_at(state)
            host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})
            if self._scanner(host).scan() == []:
                blocking.append(state)

        assert set(blocking) == set(Ticket.State.values) - {Ticket.State.IGNORED}


class IssueIntakeAgeOrderingTests(_PublicRepoTestCase):
    """A free slot goes to the issue that has waited longest (#4238).

    The forge is asked for created-ascending, but the scanner merges a per-author
    fan-out with the label query — so the ORDER that decides which issue gets the
    slot is the order of the merge, not of any single query.
    """

    OLD = "https://github.com/souliane/teatree/issues/4188"
    NEW = "https://github.com/souliane/teatree/issues/4234"
    NEWEST = "https://github.com/souliane/teatree/issues/4239"

    def test_the_oldest_issue_wins_the_slot_across_the_merged_queries(self) -> None:
        """The single free slot goes to the oldest, though its query ran LAST.

        ``sorted(trusted)`` runs OWNER's query first, so putting the newer issue there
        is what an arbitrary merge reaches first — the shape of the observed incident.
        """
        host = _Host(
            authored={
                OWNER: [_issue(self.NEW, author=OWNER, created_at="2026-08-04T09:00:00Z")],
                COLLEAGUE: [_issue(self.OLD, author=COLLEAGUE, created_at="2026-08-01T09:00:00Z")],
            },
        )

        signals = self._scanner(host, max_concurrent=1).scan()

        assert [s.payload["url"] for s in signals] == [self.OLD]

    def test_an_old_labelled_issue_outranks_a_new_authored_one(self) -> None:
        """The label query runs last of all — age still decides, not query position."""
        labelled = _issue(self.OLD, author=STRANGER, created_at="2026-08-01T09:00:00Z", labels=[self.LABEL])
        host = _Host(
            authored={OWNER: [_issue(self.NEW, author=OWNER, created_at="2026-08-04T09:00:00Z")]},
            labeled={self.LABEL: [labelled]},
        )

        signals = self._scanner(host, max_concurrent=1).scan()

        assert [s.payload["url"] for s in signals] == [self.OLD]

    def test_every_candidate_is_claimed_in_age_order(self) -> None:
        """Each query arrives NEWEST first — the forge default the fix stops trusting."""
        host = _Host(
            authored={
                OWNER: [
                    _issue(self.NEWEST, author=OWNER, created_at="2026-08-05T09:00:00Z"),
                    _issue(self.NEW, author=OWNER, created_at="2026-08-04T09:00:00Z"),
                ],
                COLLEAGUE: [_issue(self.OLD, author=COLLEAGUE, created_at="2026-08-01T09:00:00Z")],
            },
        )

        signals = self._scanner(host, max_concurrent=0).scan()

        assert [s.payload["url"] for s in signals] == [self.OLD, self.NEW, self.NEWEST]

    def test_an_issue_with_no_filing_date_sorts_behind_every_dated_one(self) -> None:
        """A payload-shape change degrades to arrival order — it never overtakes a dated issue."""
        host = _Host(
            authored={
                OWNER: [_issue(self.NEWEST, author=OWNER)],
                COLLEAGUE: [_issue(self.OLD, author=COLLEAGUE, created_at="2026-08-01T09:00:00Z")],
            },
        )

        signals = self._scanner(host, max_concurrent=0).scan()

        assert [s.payload["url"] for s in signals] == [self.OLD, self.NEWEST]


class IssueIntakeNoStarvationTests(_PublicRepoTestCase):
    """An issue admissible across many full-budget rounds is claimed the moment a slot frees.

    The observed defect: three issues filed in the morning were still unadmitted at the
    end of the day, because both slots that freed went to issues filed hours later.
    """

    OLD = "https://github.com/souliane/teatree/issues/4188"

    def _round(self, host: _Host) -> list[str]:
        return [str(signal.payload["url"]) for signal in self._scanner(host, max_concurrent=1).scan()]

    def test_the_old_issue_takes_the_first_slot_that_frees(self) -> None:
        held = "https://github.com/souliane/teatree/issues/1"
        ImplementedIssueMarker.objects.claim(held, overlay=self.OVERLAY)
        queue = [_issue(self.OLD, author=OWNER, created_at="2026-08-01T09:00:00Z")]
        host = _Host(authored={OWNER: queue})

        # Three rounds at a full budget, each with a fresher issue arriving. Each one
        # lands at the FRONT of the query result, as the forge's own default order puts it.
        for day in (2, 3, 4):
            queue.insert(
                0,
                _issue(
                    f"https://github.com/souliane/teatree/issues/42{day}0",
                    author=OWNER,
                    created_at=f"2026-08-0{day}T09:00:00Z",
                ),
            )
            assert self._round(host) == []

        # The held slot turns over exactly as it did in the incident.
        ImplementedIssueMarker.objects.filter(issue_url=held).delete()

        assert self._round(host) == [self.OLD]


class IssueIntakeStarvationVisibilityTests(_PublicRepoTestCase):
    """Every admissible candidate the budget passed over is recorded, not silently skipped."""

    OLD = "https://github.com/souliane/teatree/issues/4188"
    NEW = "https://github.com/souliane/teatree/issues/4234"

    def test_a_full_budget_records_every_passed_over_candidate(self) -> None:
        ImplementedIssueMarker.objects.claim("https://github.com/souliane/teatree/issues/1", overlay=self.OVERLAY)
        host = _Host(
            authored={
                OWNER: [
                    _issue(self.OLD, author=OWNER, created_at="2026-08-01T09:00:00Z"),
                    _issue(self.NEW, author=OWNER, created_at="2026-08-04T09:00:00Z"),
                ],
            },
        )

        assert self._scanner(host, max_concurrent=1).scan() == []

        waiting = UnclaimedIntakeCandidate.objects.filter(overlay=self.OVERLAY)
        assert sorted(waiting.values_list("issue_url", flat=True)) == sorted([self.OLD, self.NEW])
        assert waiting.get(issue_url=self.OLD).issue_created_at == dt.datetime(2026, 8, 1, 9, tzinfo=dt.UTC)

    def test_an_unclaimable_tick_still_records_the_queue_it_cannot_act_on(self) -> None:
        """``can_claim=False`` is the state the incident sat in all day — it must still witness."""
        host = _Host(authored={OWNER: [_issue(self.OLD, author=OWNER, created_at="2026-08-01T09:00:00Z")]})

        assert self._scanner(host, can_claim=False).scan() == []

        assert list(UnclaimedIntakeCandidate.objects.values_list("issue_url", flat=True)) == [self.OLD]

    def test_a_claimed_candidate_leaves_the_waiting_ledger(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.OLD, author=OWNER, created_at="2026-08-01T09:00:00Z")]})
        UnclaimedIntakeCandidate.objects.sync(self.OVERLAY, [WaitingCandidate(issue_url=self.OLD)])

        assert len(self._scanner(host, max_concurrent=1).scan()) == 1

        assert not UnclaimedIntakeCandidate.objects.exists()

    def test_a_claim_refused_by_an_existing_holder_is_not_reported_as_starved(self) -> None:
        """A refused claim means somebody holds the issue — that is not a passed-over candidate."""
        ImplementedIssueMarker.objects.claim(self.OLD, overlay=self.OVERLAY)
        host = _Host(authored={OWNER: [_issue(self.OLD, author=OWNER, created_at="2026-08-01T09:00:00Z")]})

        assert self._scanner(host, max_concurrent=0).scan() == []

        assert not UnclaimedIntakeCandidate.objects.exists()


class IssueIntakePayloadReadersTests(_PublicRepoTestCase):
    """The three payload readers the fail-closed gate leans on."""

    def test_issue_url_reads_both_forge_url_fields(self) -> None:
        assert issue_url({"web_url": self.URL_A}) == self.URL_A
        assert issue_url({"html_url": self.URL_A}) == self.URL_A
        assert issue_url({}) == ""

    def test_issue_author_reads_both_forge_shapes_and_strips(self) -> None:
        assert issue_author({"user": {"login": " souliane "}}) == OWNER
        assert issue_author({"author": {"username": OWNER}}) == OWNER
        assert issue_author({"user": "not-a-dict"}) == ""

    def test_issue_created_at_reads_both_forges_and_degrades_to_none(self) -> None:
        assert issue_created_at({"created_at": "2026-08-01T09:00:00Z"}) == dt.datetime(2026, 8, 1, 9, tzinfo=dt.UTC)
        # GitLab emits sub-second precision and a numeric offset.
        assert issue_created_at({"created_at": "2026-08-01T11:00:00.000+02:00"}) == dt.datetime(
            2026, 8, 1, 9, tzinfo=dt.UTC
        )
        # A naive stamp is read as UTC, not as local time.
        assert issue_created_at({"created_at": "2026-08-01T09:00:00"}) == dt.datetime(2026, 8, 1, 9, tzinfo=dt.UTC)
        assert issue_created_at({"created_at": "not a date"}) is None
        assert issue_created_at({"created_at": 1234}) is None
        assert issue_created_at({}) is None

    def test_author_is_trusted_refuses_an_unresolvable_author(self) -> None:
        trusted = frozenset({OWNER})

        assert author_is_trusted(_issue(self.URL_A, author=OWNER), trusted)
        assert not author_is_trusted(_issue(self.URL_A, author=STRANGER), trusted)
        assert not author_is_trusted({"web_url": self.URL_A}, trusted)
