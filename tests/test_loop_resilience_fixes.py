"""RED-first tests for loop/messaging resilience fixes.

Five confirmed bugs reproduced here before any fix is applied. Each test
class is self-contained and targets a single finding. Run these against
the unfixed code to see them go RED, then apply the fix and confirm GREEN.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.backend_protocols import DraftState
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from teatree.messaging.notify_with_fallback import notify_with_fallback
from tests._forge_stub import merge_path_stdout

# ---------------------------------------------------------------------------
# Helpers shared across findings
# ---------------------------------------------------------------------------

_PRIMARY_TARGET = "teatree.messaging.notify_with_fallback.notify_user"
_FALLBACK_TARGET = "teatree.messaging.notify_with_fallback.messaging_from_overlay"


def _delivering_backend() -> Any:
    from unittest.mock import MagicMock  # noqa: PLC0415

    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000100"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000100"
    b.fetch_message.return_value = {"ts": "1700000000.000100", "text": "the body"}
    return b


# ---------------------------------------------------------------------------
# F1: notify_with_fallback must not raise on OperationalError
# ---------------------------------------------------------------------------


class TestF1NeverRaiseDatabaseError(TestCase):
    """F1 — _stamp_transport / _upsert_botping catch only IntegrityError.

    Any other django.db.DatabaseError (OperationalError, etc.) propagates
    and violates the module contract 'Never raises into the calling turn'.
    """

    def test_stamp_transport_operational_error_does_not_raise(self) -> None:
        """BotPing.objects.filter().update() raises OperationalError in _stamp_transport.

        _stamp_transport must catch DatabaseError subclasses; notify_with_fallback
        must not see the exception (never-raise contract).
        """
        from unittest.mock import MagicMock  # noqa: PLC0415

        # Patch the queryset that _stamp_transport calls to raise OperationalError.
        mock_qs = MagicMock()
        mock_qs.update.side_effect = OperationalError("disk full")

        with (
            patch(_PRIMARY_TARGET, return_value=True),
            patch(
                "teatree.messaging.notify_with_fallback.BotPing.objects.filter",
                return_value=mock_qs,
            ),
        ):
            # Must not raise — contract is "never raises into the calling turn"
            result = notify_with_fallback(
                "hello",
                kind="info",
                idempotency_key="f1-stamp-opererr",
                audience=NotifyAudience.OWNER_DELIVERY,
                user_id="U_ME",
            )
        # Primary DID deliver; the key is no exception was raised.
        assert result is not None

    def test_upsert_botping_operational_error_returns_not_delivered(self) -> None:
        """OperationalError in _upsert_botping must not escape notify_with_fallback."""
        # Make the fallback path trigger an OperationalError when it tries to
        # write to BotPing. We patch the underlying queryset so the OperationalError
        # surfaces inside _upsert_botping's transaction.atomic() block.
        with (
            patch(_PRIMARY_TARGET, return_value=False),
            patch(_FALLBACK_TARGET, return_value=None),
            patch(
                "teatree.messaging.notify_with_fallback.BotPing.objects.update_or_create",
                side_effect=OperationalError("database locked"),
            ),
        ):
            # Must not raise
            result = notify_with_fallback(
                "hello",
                kind="info",
                idempotency_key="f1-upsert-opererr",
                audience=NotifyAudience.OWNER_DELIVERY,
                user_id="U_ME",
            )
        assert result.delivered is False


# ---------------------------------------------------------------------------
# F3: slack_mentions must record drained DMs before commit_drain discards the backing file
# ---------------------------------------------------------------------------


class TestF3SlackMentionsRecordsDrainedDmsBeforeCommit(TestCase):
    """F3 — a drained DM is durably recorded before the drain is committed.

    ``commit_drain`` deletes the ``.draining`` file; once it runs, a DM that was
    not yet persisted can never be recovered by the next drain.
    """

    def test_drained_dm_row_exists_when_the_drain_commits(self) -> None:
        backend = MagicMock()
        backend.user_id = "U_HUMAN"
        backend.fetch_mentions.return_value = []
        backend.auth_test.return_value = {"ok": True, "user_id": "U_BOT", "bot_id": "B_BOT"}
        backend.get_permalink.return_value = ""
        dm = {
            "type": "message",
            "channel_type": "im",
            "ts": "1700000099.000001",
            "user": "U_HUMAN",
            "channel": "D1",
            "text": "ping",
        }
        rows_at_commit: list[int] = []

        with (
            patch("teatree.backends.slack.receiver.drain_event_queue", return_value=[{"event": dm}]),
            patch(
                "teatree.backends.slack.receiver.commit_drain",
                side_effect=lambda **_kwargs: rows_at_commit.append(PendingChatInjection.objects.count()),
            ),
        ):
            SlackMentionsScanner(backend=backend).scan()

        assert rows_at_commit == [1]


# ---------------------------------------------------------------------------
# F4: gitlab_approvals _record_emission must not create phantom blank-overlay Ticket
# ---------------------------------------------------------------------------


class TestF4GitlabApprovalsNoPhantomBlankOverlayTicket(TestCase):
    """F4 — _record_emission does get_or_create with overlay='' as default.

    When the URL maps to no existing Ticket, a phantom Ticket(overlay='')
    is created. Fix: use filter().first() and skip if no ticket exists,
    or pass the scanner's real overlay.
    """

    # ast-grep-ignore: ac-django-no-pytest-django-db
    pytestmark = pytest.mark.django_db

    def test_no_phantom_blank_overlay_ticket_created(self) -> None:
        """No phantom blank-overlay Ticket created for an unmapped URL.

        _record_emission with a URL that has no existing Ticket row must not
        create a new Ticket(overlay='').
        """
        from teatree.core.models import Ticket  # noqa: PLC0415
        from teatree.loop.scanners.gitlab_approvals import _record_emission  # noqa: PLC0415

        url = "https://gitlab.example/no-overlay/-/merge_requests/9999"
        assert not Ticket.objects.filter(issue_url=url).exists()

        _record_emission(url, "abc123")

        # Bug: a phantom Ticket(overlay='') is created.
        # Fix: no Ticket should be created if none existed.
        phantoms = Ticket.objects.filter(issue_url=url, overlay="")
        assert not phantoms.exists(), f"phantom blank-overlay Ticket was created: {list(phantoms.values())}"


# ---------------------------------------------------------------------------
# F7: pr_sweep solo-overlay squash merge must be SHA-bound (#1985) and never
# return a misleading empty SHA on success.
# ---------------------------------------------------------------------------


class TestF7PrSweepBoundSquashSurfacesSha(TestCase):
    """F7 — the solo-overlay squash merge is bound and returns a non-empty SHA.

    The former unbound ``merge_pr_squash`` followed the merge with a separate
    ``gh pr view mergeCommit`` whose rc!=0 yielded a silently empty SHA (the F7
    bug). Option A (#1985) routes the merge through ``execute_bound_merge``,
    which returns ``merged_sha or expected_head_oid`` from the merge response
    itself — never a silent empty on success, and now SHA-bound so a force-push
    in the TOCTOU window can't slip an unreviewed head through.
    """

    def test_bound_merge_returns_non_empty_sha_on_success(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.review import author_trust  # noqa: PLC0415
        from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient  # noqa: PLC0415
        from tests.teatree_core.conftest import record_owned_pr_for_test, seed_merge_safe_verdict  # noqa: PLC0415

        expected = "c" * 40
        # The bound merge runs the #2829 review-verdict gate; seed the verdict.
        record_owned_pr_for_test(slug="owner/repo", pr_id=42, head_sha=expected)
        seed_merge_safe_verdict(slug="owner/repo", pr_id=42, sha=expected)

        def _gh(argv: list[str]) -> tuple[int, str, str]:
            joined = " ".join(argv)
            if "pulls" in joined and "merge" in joined:
                # The merge response carries no ``sha`` field — the bound path
                # must fall back to the bound head, never a silent empty string.
                return (0, "{}", "")
            return (0, merge_path_stdout(joined), "")

        client = GhPrApiClient()
        # The #18 floor re-reads the live not-draft + required-checks state at the
        # merge chokepoint; a non-draft, green head clears it so the bound merge's
        # sha-fallback contract is what gets exercised here. #3313 hardened the
        # #3244 provenance gate so a same-repo head is no longer trusted
        # unconditionally — it is conjoined with the identity+visibility author
        # check (mirroring test_merge_execution_author_gate's
        # test_same_repo_on_internal_repo_passes). This test is about the
        # SHA-fallback contract, not provenance, so it takes the internal-repo
        # bypass rather than asserting on a specific trusted author.
        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_draft_state", return_value=DraftState.NOT_DRAFT),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.required_checks_status", return_value="green"),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_same_repo", return_value=True),
            patch.object(author_trust, "repo_is_internal", return_value=True),
            patch("teatree.backends.github.client.GitHubCodeHost.current_user", return_value="merge-bot"),
        ):
            result = client.merge_pr_bound(slug="owner/repo", pr_id=42, expected_head_oid=expected)

        assert result.merged is True
        assert result.merged_sha != "", "merge_pr_bound returned an empty SHA on a successful merge"
        assert result.merged_sha == expected


class TestPrSweepListLimit(TestCase):
    """``gh pr list`` must request more than the 30-PR default cap."""

    def test_list_open_prs_passes_limit_at_least_100(self) -> None:
        from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient  # noqa: PLC0415

        captured: list[list[str]] = []

        class _FakeClient(GhPrApiClient):
            __slots__ = ()

            def _run_gh(self, argv: list[str], *, slug: str) -> tuple[int, str, str]:
                del slug
                captured.append(argv)
                return 0, "[]", ""

        _FakeClient().list_open_prs(slug="owner/repo")

        assert captured, "list_open_prs never shelled out to gh"
        argv = captured[0]
        assert "--limit" in argv, f"no --limit in argv: {argv}"
        limit = int(argv[argv.index("--limit") + 1])
        assert limit >= 100, f"--limit {limit} below the 100 convention"
