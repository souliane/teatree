"""The GitLab assigned-issue sync intake — ``fetch_assigned_issues``.

Covers the two obligations of the intake: it classifies ``Ticket.kind`` at create
time (#17), and it reconciles an already-tracked issue against upstream truth so a
row created before a field existed still ends up carrying it.

``fetch_assigned_issues`` is the primary real-defect intake — a board issue
carrying a ``bug`` label (or a ``fix …`` title) must be created as FIX, not
FEATURE. Classification is create-only, so a mis-classified sync ticket can never
be reclassified: S2 would stay blind and the fix-record DoD gate would never fire.
"""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.backends.gitlab.sync_issues import fetch_assigned_issues
from teatree.core.intake.label_admission import LabelPolicy
from teatree.core.models import Ticket
from teatree.types import SyncResult


class TestGitLabAssignedIssueSyncClassifiesKind(TestCase):
    def _synced_ticket(self, *, url: str, title: str, labels: list[str]) -> Ticket:
        host = MagicMock()
        host.list_assigned_issues.return_value = [{"web_url": url, "title": title, "labels": labels}]
        fetch_assigned_issues(host, "me", SyncResult(), overlay_name="acme")
        return Ticket.objects.get(issue_url=url)

    def test_bug_labeled_issue_is_fix(self) -> None:
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/711",
            title="Login button unresponsive",
            labels=["bug"],
        )
        assert ticket.kind == Ticket.Kind.FIX

    def test_fix_titled_issue_is_fix(self) -> None:
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/712",
            title="fix: crash on empty export",
            labels=[],
        )
        assert ticket.kind == Ticket.Kind.FIX

    def test_plain_feature_issue_is_feature(self) -> None:
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/713",
            title="Add dark mode toggle",
            labels=["enhancement"],
        )
        assert ticket.kind == Ticket.Kind.FEATURE

    def test_substring_lookalike_label_stays_feature(self) -> None:
        # A "debug" label must NOT flip a feature to FIX (token-boundary matching).
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/714",
            title="Improve the debug console",
            labels=["debug"],
        )
        assert ticket.kind == Ticket.Kind.FEATURE


class TestGitLabAssignedIssueSyncHonoursTheLabelGate(TestCase):
    """The second intake answers to the same allowlist as the ``assigned_issues`` scanner.

    Assignment alone is not a nomination — an issue the operator never labelled
    ready must not become a Ticket row here just because it is assigned.
    """

    URL = "https://gitlab.com/o/r/-/issues/720"

    def _sync(self, *, labels: list[str], ready: tuple[str, ...], exclude: tuple[str, ...] = ()) -> None:
        host = MagicMock()
        host.list_assigned_issues.return_value = [{"web_url": self.URL, "title": "Some issue", "labels": labels}]
        fetch_assigned_issues(
            host,
            "me",
            SyncResult(),
            overlay_name="acme",
            label_policy=LabelPolicy(ready_labels=ready, exclude_labels=exclude),
        )

    def test_issue_without_a_ready_label_creates_no_ticket(self) -> None:
        self._sync(labels=["backend"], ready=("ready-for-dev",))

        assert not Ticket.objects.filter(issue_url=self.URL).exists()

    def test_issue_with_a_ready_label_creates_a_ticket(self) -> None:
        self._sync(labels=["ready-for-dev"], ready=("ready-for-dev",))

        assert Ticket.objects.filter(issue_url=self.URL).exists()

    def test_excluded_issue_creates_no_ticket(self) -> None:
        self._sync(labels=["ready-for-dev", "blocked"], ready=("ready-for-dev",), exclude=("blocked",))

        assert not Ticket.objects.filter(issue_url=self.URL).exists()

    def test_empty_allowlist_still_admits_everything(self) -> None:
        self._sync(labels=["backend"], ready=())

        assert Ticket.objects.filter(issue_url=self.URL).exists()


class TestGitLabAssignedIssueSyncBackfillsTheIssueTitle(TestCase):
    """An already-tracked issue is reconciled against upstream truth, not skipped.

    ``extra["issue_title"]`` was written on the create path only, so a row created
    before the key existed — or by any other intake — never acquired a title and
    every consumer that summarises from it (the board's card description) had
    nothing to render, forever. The title is upstream truth, refreshed on every
    sync the same way :func:`apply_issue_data` refreshes it; local decisions
    (``state``, ``kind``, and an already-written ``short_description``) are never
    touched here. Seeding a *blank* ``short_description`` from that title is
    covered by :class:`TestGitLabAssignedIssueSyncSeedsTheCardLabel`.
    """

    URL = "https://gitlab.com/o/r/-/issues/730"

    def _sync(self, *, title: str) -> SyncResult:
        host = MagicMock()
        host.list_assigned_issues.return_value = [{"web_url": self.URL, "title": title, "labels": []}]
        result = SyncResult()
        fetch_assigned_issues(host, "me", result, overlay_name="acme")
        return result

    def test_existing_ticket_without_a_title_gets_one(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["r"], extra={})

        result = self._sync(title="Login button unresponsive")

        ticket.refresh_from_db()
        assert ticket.extra["issue_title"] == "Login button unresponsive"
        assert result.tickets_updated == 1

    def test_existing_title_is_refreshed_from_upstream(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["r"], extra={"issue_title": "Old wording"})

        self._sync(title="New wording")

        ticket.refresh_from_db()
        assert ticket.extra["issue_title"] == "New wording"

    def test_other_extra_keys_survive_the_backfill(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["r"], extra={"tracker_status": "Process::Doing"})

        self._sync(title="Login button unresponsive")

        ticket.refresh_from_db()
        assert ticket.extra["tracker_status"] == "Process::Doing"
        assert ticket.extra["issue_title"] == "Login button unresponsive"

    def test_an_unchanged_title_is_not_counted_as_an_update(self) -> None:
        Ticket.objects.create(
            issue_url=self.URL,
            repos=["r"],
            short_description="Same wording",
            extra={"issue_title": "Same wording"},
        )

        result = self._sync(title="Same wording")

        assert result.tickets_updated == 0

    def test_an_empty_upstream_title_does_not_erase_the_stored_one(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["r"], extra={"issue_title": "Known wording"})

        self._sync(title="")

        ticket.refresh_from_db()
        assert ticket.extra["issue_title"] == "Known wording"

    def test_a_new_repo_is_still_recorded_alongside_the_title(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["other"], extra={})

        result = self._sync(title="Login button unresponsive")

        ticket.refresh_from_db()
        assert ticket.repos == ["other", "r"]
        assert ticket.extra["issue_title"] == "Login button unresponsive"
        assert result.tickets_updated == 1


class TestGitLabAssignedIssueSyncSeedsTheCardLabel(TestCase):
    """Backfilling the title must also give the card something to render.

    The board renders ``Ticket.short_description``, not ``extra["issue_title"]``
    — a row with a title and a blank ``short_description`` still shows
    ``(no description)``. The create path never has that gap
    (:meth:`Ticket.stamp_issue_title` writes both), so a sync that reconciles an
    already-tracked issue by writing ``extra`` alone leaves the very cards the
    backfill exists to fix exactly as blank as it found them.

    A blank ``short_description`` is an absence, not an operator decision, so it
    is seeded from the forge title; a value the operator (or the summariser)
    already wrote is never overwritten.
    """

    URL = "https://gitlab.com/o/r/-/issues/731"

    def _sync(self, *, title: str) -> SyncResult:
        host = MagicMock()
        host.list_assigned_issues.return_value = [{"web_url": self.URL, "title": title, "labels": []}]
        result = SyncResult()
        fetch_assigned_issues(host, "me", result, overlay_name="acme")
        return result

    def test_a_backfilled_title_also_seeds_the_card_label(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["r"], extra={})

        self._sync(title="Login button unresponsive")

        ticket.refresh_from_db()
        assert ticket.short_description == "Login button unresponsive"

    def test_a_blank_card_label_is_seeded_even_when_the_title_is_unchanged(self) -> None:
        # The rows the earlier title-only backfill already touched: they carry a
        # title, so the title write is a no-op, yet the card is still blank.
        ticket = Ticket.objects.create(
            issue_url=self.URL,
            repos=["r"],
            extra={"issue_title": "Login button unresponsive"},
        )

        result = self._sync(title="Login button unresponsive")

        ticket.refresh_from_db()
        assert ticket.short_description == "Login button unresponsive"
        assert result.tickets_updated == 1

    def test_an_operator_edited_card_label_survives_the_sync(self) -> None:
        ticket = Ticket.objects.create(
            issue_url=self.URL,
            repos=["r"],
            short_description="my own words",
            extra={},
        )

        self._sync(title="Login button unresponsive")

        ticket.refresh_from_db()
        assert ticket.short_description == "my own words"
        assert ticket.extra["issue_title"] == "Login button unresponsive"

    def test_a_long_title_is_truncated_to_the_column_width(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["r"], extra={})
        max_len = Ticket._meta.get_field("short_description").max_length or 80

        self._sync(title="x" * (max_len + 50))

        ticket.refresh_from_db()
        assert len(ticket.short_description) == max_len
        assert ticket.extra["issue_title"] == "x" * (max_len + 50)

    def test_an_empty_upstream_title_seeds_nothing(self) -> None:
        ticket = Ticket.objects.create(issue_url=self.URL, repos=["r"], extra={})

        self._sync(title="")

        ticket.refresh_from_db()
        assert ticket.short_description == ""
