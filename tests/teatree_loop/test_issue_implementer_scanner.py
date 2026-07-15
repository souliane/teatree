"""Behaviour tests for ``IssueImplementerScanner`` — author-trust intake + claim idempotency (#3235).

The scanner is the discovery + claim half of the always-on issue-implementer loop.
Intake is decided by the trusted AUTHOR of the issue (#3235), not by a
hand-applied label: the scanner lists each trusted author's open issues via the
code-host backend's author-scoped query, REFUSES any issue whose author is not in
the trusted set (fail-closed), and claims the rest through the TOCTOU-safe
:meth:`ImplementedIssueMarker.claim` so a re-tick (or a concurrent overlay) never
double-dispatches the same issue.

The legacy label filter survives as an OPT-IN (``require_label=True``) — see
:class:`IssueImplementerRequireLabelTests`.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import ImplementedIssueMarker, TrustedIdentity
from teatree.loop.scanners.issue_implementer import IssueImplementerScanner
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
    open_prs: list[RawAPIDict] = field(default_factory=list)
    merged_prs: list[RawAPIDict] = field(default_factory=list)
    #: Every author handle the scanner asked the forge about — the intake surface.
    queried_authors: list[str] = field(default_factory=list)
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

    def list_my_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return self.open_prs

    def list_my_merged_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return self.merged_prs


def _issue(url: str, *, author: str, labels: list[str] | None = None, state: str = "open") -> RawAPIDict:
    """A GitHub-shaped issue payload (``user.login`` is the author)."""
    return {"web_url": url, "title": "do it", "labels": labels or [], "state": state, "user": {"login": author}}


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

    def _scanner(self, host: _Host, **overrides: object) -> IssueImplementerScanner:
        kwargs: dict[str, object] = {
            "host": host,
            "label": self.LABEL,
            "overlay_name": self.OVERLAY,
            "trusted_authors": TRUSTED,
            "identities": (OWNER,),
        }
        kwargs.update(overrides)
        return IssueImplementerScanner(**kwargs)


class IssueImplementerAuthorTrustIntakeTests(_PublicRepoTestCase):
    """Intake is by TRUSTED AUTHOR — no label required (#3235)."""

    def test_owner_authored_unlabelled_issue_is_claimed_and_emitted(self) -> None:
        """The owner's own issue (a ``user_identity_aliases`` handle), with NO label, is claimed."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        signals = self._scanner(host).scan()

        assert [s.kind for s in signals] == ["issue_implementer.claimed"]
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


class IssueImplementerUntrustedAuthorRefusalTests(_PublicRepoTestCase):
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

    def test_stranger_authored_issue_is_refused_even_when_labelled(self) -> None:
        """A label can never launder an untrusted author — the label is not an authority."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=STRANGER, labels=[self.LABEL])]})

        assert self._scanner(host).scan() == []
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


class IssueImplementerNeedsTriageGateTests(_PublicRepoTestCase):
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


class IssueImplementerRequireLabelTests(_PublicRepoTestCase):
    """Back-compat: ``require_label=True`` restores the label as a MANDATORY second gate.

    An operator who wants the pre-#3235 hand-tagged workflow flips
    ``issue_implementer_require_label`` on; the label filter then applies ON TOP of
    author trust (never instead of it — trust is not optional).
    """

    def test_label_is_required_when_the_flag_is_on(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        assert self._scanner(host, require_label=True).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_labelled_trusted_issue_is_claimed_when_the_flag_is_on(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER, labels=[self.LABEL])]})

        signals = self._scanner(host, require_label=True).scan()

        assert [s.payload["url"] for s in signals] == [self.URL_A]

    def test_dict_shaped_label_is_matched_when_the_flag_is_on(self) -> None:
        issue = _issue(self.URL_A, author=OWNER)
        issue["labels"] = [{"name": self.LABEL}]
        host = _Host(authored={OWNER: [issue]})

        assert len(self._scanner(host, require_label=True).scan()) == 1

    def test_label_still_cannot_launder_an_untrusted_author(self) -> None:
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=STRANGER, labels=[self.LABEL])]})

        assert self._scanner(host, require_label=True).scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_empty_label_with_the_flag_on_claims_nothing(self) -> None:
        """Defence-in-depth: require_label + no label configured = nothing is claimable."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER, labels=[self.LABEL])]})

        assert self._scanner(host, require_label=True, label="").scan() == []
        assert not ImplementedIssueMarker.objects.exists()

    def test_empty_label_with_the_flag_off_still_claims_by_author(self) -> None:
        """The label is NOT required by default — an unset label is no longer a kill-switch."""
        host = _Host(authored={OWNER: [_issue(self.URL_A, author=OWNER)]})

        assert len(self._scanner(host, label="").scan()) == 1


class IssueImplementerClaimLifecycleTests(_PublicRepoTestCase):
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


class IssueImplementerReadbackTests(_PublicRepoTestCase):
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


class IssueImplementerRepoScopeTests(_PublicRepoTestCase):
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
