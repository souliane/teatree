"""``review_request_post`` — sanctioned authorized review-request post (#1098).

The post-half of #1084/#1094: one classifier-legible command that runs
the #1094 live-channel dedup, requires a #960 recorded approval, then
posts. These tests mock ONLY the network boundary (the messaging backend
``post_message``/``get_permalink`` and the live-read guard) — the #960
approval/audit bookkeeping and the Risk-c orphan-claim rollback run for
real against the DB.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import OnBehalfPostMode, UserSettings
from teatree.core.gates.review_request_guard import GuardDecision, GuardTarget
from teatree.core.models import OnBehalfApproval, OnBehalfAudit, ReviewRequestPost, Ticket

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_TARGET = GuardTarget(channel_id="C_REVIEW", channel_name="the-review-team", token="xoxp")
_CMD = "teatree.core.management.commands.review_request_post"
_SHA = "a" * 40


class _FakeBackend:
    """Minimal SlackBotBackend stand-in: records the one post, no network."""

    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": "1.23"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://team.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def _run(*extra: str) -> tuple[int, dict[str, object]]:
    """Call the command, capture exit code + the machine-legible dict it prints."""
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            call_command("review_request_post", "--mr-url", _MR_URL, "--approver", "souliane", *extra)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    out = buf.getvalue()
    # The dict line is the last JSON object printed.
    payload: dict[str, object] = {}
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("{"):
            payload = json.loads(line)
    return code, payload


class _DataDirMixin:
    """Isolate ``T3_DATA_DIR`` to a tmp dir for tests whose path posts."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = Path(tempfile.mkdtemp())
        self._prev_data_dir = os.environ.get("T3_DATA_DIR")
        os.environ["T3_DATA_DIR"] = str(self._tmp)

    def tearDown(self) -> None:
        if self._prev_data_dir is None:
            os.environ.pop("T3_DATA_DIR", None)
        else:
            os.environ["T3_DATA_DIR"] = self._prev_data_dir
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()


def _gate_required(*, required: bool) -> AbstractContextManager[object]:
    return patch(
        "teatree.core.gates.anti_vacuity_gate.get_effective_settings",
        return_value=UserSettings(require_anti_vacuity_attestation=required),
    )


class TestReviewRequestPostAntiVacuityGate(_DataDirMixin, TestCase):
    """#1829: with the gate on, the post refuses before any dedup claim / wire call."""

    def test_refused_without_attestation_and_takes_no_claim(self) -> None:
        backend = _FakeBackend()
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        with (
            _gate_required(required=True),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--ticket-id", str(ticket.pk), "--head-sha", _SHA)
        assert code == 2
        assert payload["action"] == "refused"
        assert payload["reason"] == "anti_vacuity_not_attested"
        assert backend.posts == []
        # The gate runs first — no ReviewRequestPost claim was taken.
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

    def test_refused_when_ticket_id_or_head_sha_missing(self) -> None:
        with (
            _gate_required(required=True),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
        ):
            code, payload = _run()  # no --ticket-id / --head-sha
        assert code == 2
        assert payload["reason"] == "anti_vacuity_not_attested"

    def test_allows_with_bound_attestation(self) -> None:
        OnBehalfApproval.record(target=_MR_URL, action="review_request_post", approver_id="souliane")
        backend = _FakeBackend()
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        ticket.record_anti_vacuity_attestation(_SHA, "AC1-3 mapped", ["tests/x.py::test_y"])
        with (
            _gate_required(required=True),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=GuardDecision(action="post")),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--ticket-id", str(ticket.pk), "--head-sha", _SHA, "--title", "t")
        assert code == 0, payload
        assert payload["action"] == "post"
        assert len(backend.posts) == 1

    def test_noop_when_gate_off_ignores_missing_attestation(self) -> None:
        OnBehalfApproval.record(target=_MR_URL, action="review_request_post", approver_id="souliane")
        backend = _FakeBackend()
        with (
            _gate_required(required=False),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=GuardDecision(action="post")),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--title", "t")
        assert code == 0, payload
        assert payload["action"] == "post"


def _reviewed_gate(*, required: bool) -> AbstractContextManager[object]:
    return patch(
        "teatree.core.gates.review_request_state_gate.get_effective_settings",
        return_value=UserSettings(require_reviewed_state_for_review_request=required),
    )


class TestReviewRequestPostReviewedStateGate(_DataDirMixin, TestCase):
    """PR-08: with the gate on, a broadcast refuses unless the ticket is REVIEWED + has evidence."""

    def test_refused_when_ticket_not_reviewed_and_takes_no_claim(self) -> None:
        backend = _FakeBackend()
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.CODED)
        with (
            _reviewed_gate(required=True),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--ticket-id", str(ticket.pk))
        assert code == 2
        assert payload["reason"] == "ticket_not_reviewed"
        assert backend.posts == []
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

    def test_refused_when_ticket_id_missing(self) -> None:
        with (
            _reviewed_gate(required=True),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
        ):
            code, payload = _run()  # no --ticket-id
        assert code == 2
        assert payload["reason"] == "ticket_not_reviewed"

    def test_allows_reviewed_ticket_with_evidence(self) -> None:
        from teatree.core.models import ReviewEvidence  # noqa: PLC0415

        OnBehalfApproval.record(target=_MR_URL, action="review_request_post", approver_id="souliane")
        backend = _FakeBackend()
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEWED)
        ReviewEvidence.record(
            ticket=ticket,
            kind=ReviewEvidence.Kind.COLD_REVIEW,
            reviewer_identity="reviewer-bob",
            verdict="merge_safe",
            head_sha=_SHA,
        )
        with (
            _reviewed_gate(required=True),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=GuardDecision(action="post")),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--ticket-id", str(ticket.pk), "--title", "t")
        assert code == 0, payload
        assert payload["action"] == "post"
        assert len(backend.posts) == 1

    def test_noop_when_gate_off(self) -> None:
        OnBehalfApproval.record(target=_MR_URL, action="review_request_post", approver_id="souliane")
        backend = _FakeBackend()
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.CODED)
        with (
            _reviewed_gate(required=False),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=GuardDecision(action="post")),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--ticket-id", str(ticket.pk), "--title", "t")
        assert code == 0, payload
        assert payload["action"] == "post"


class TestReviewRequestPostDedup(TestCase):
    def test_no_review_channel_or_token_falls_back_to_draft_dm(self) -> None:
        """#2231: unpostable Connect channel → draft DM, not silent suppress."""
        notified: list[dict[str, str]] = []

        def _capture_notify(text: str, *, kind: object, idempotency_key: str, **_kw: object) -> bool:
            notified.append({"text": text, "idempotency_key": idempotency_key})
            return True

        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=None),
            patch("teatree.core.notify.notify_user", side_effect=_capture_notify),
        ):
            code, payload = _run()
        assert code == 0
        assert payload["action"] == "draft"
        assert payload["reason"] == "no_review_channel_or_token"
        assert payload["mr_url"] == _MR_URL
        # The bot must DM the user with the review request text so they can
        # forward it manually to the review channel.
        assert len(notified) == 1
        assert _MR_URL in notified[0]["text"]

    def test_failed_dm_fallback_emits_suppress_not_draft(self) -> None:
        """Failed DM fallback must emit action=suppress, not action=draft.

        When ``notify_user`` returns ``False`` (no backend / no user_id),
        the review request notification never reached anyone. Emitting
        ``action=draft`` in that case is the same silent-loss class #2231
        targeted. The correct outcome is ``action=suppress`` so the caller
        knows the notification did not land.
        """
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=None),
            patch("teatree.core.notify.notify_user", return_value=False),
        ):
            code, payload = _run()
        assert code == 0
        # notify_user returned False → the DM was not delivered → suppress,
        # not draft.  This assertion is RED on the pre-fix code (which always
        # emits action=draft).
        assert payload["action"] == "suppress"
        assert payload["reason"] == "no_review_channel_or_token"
        assert payload["mr_url"] == _MR_URL

    def test_dedup_suppress_does_not_post(self) -> None:
        backend = _FakeBackend()
        decision = GuardDecision(
            action="suppress",
            permalink="https://team.slack.com/archives/C/p1",
            author="U_HUMAN",
            reason="already_posted",
        )
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=decision),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run()
        assert code == 0
        assert payload["action"] == "suppress"
        assert payload["reason"] == "already_posted"
        assert payload["permalink"] == "https://team.slack.com/archives/C/p1"
        assert backend.posts == []


class TestReviewRequestPostMissingApproval(_DataDirMixin, TestCase):
    """Risk-c regression: a refusal must NOT leave an orphan claim wedging future posts."""

    def test_refuses_without_approval_and_rolls_back_claim(self) -> None:
        backend = _FakeBackend()
        # Real guard would claim ReviewRequestPost; mock it to the post verdict
        # AND take the real claim so the rollback path is exercised.

        def _real_claim(*, mr_url: str, target: GuardTarget) -> GuardDecision:
            ReviewRequestPost.objects.create(mr_url=mr_url, slack_channel_id=target.channel_id, slack_thread_ts="")
            return GuardDecision(action="post")

        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", side_effect=_real_claim),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run()

        assert code == 2
        assert payload["action"] == "refused"
        assert payload["reason"] == "on_behalf_not_approved"
        assert backend.posts == []
        assert OnBehalfAudit.objects.count() == 0
        # Risk-c: the just-created claim is rolled back — no orphan row.
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

    def test_refusal_message_names_approve_on_behalf_command(self) -> None:
        backend = _FakeBackend()

        def _real_claim(*, mr_url: str, target: GuardTarget) -> GuardDecision:
            ReviewRequestPost.objects.create(mr_url=mr_url, slack_channel_id=target.channel_id, slack_thread_ts="")
            return GuardDecision(action="post")

        buf = io.StringIO()
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", side_effect=_real_claim),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
            contextlib.redirect_stdout(buf),
            pytest.raises(SystemExit),
        ):
            call_command("review_request_post", "--mr-url", _MR_URL, "--approver", "souliane")
        text = buf.getvalue()
        assert "t3 review approve-on-behalf" in text
        assert "review_request_post" in text

    def test_subsequent_approved_call_succeeds_after_refusal(self) -> None:
        """After a refusal rolled back the claim, a now-approved retry must POST.

        This is the Risk-c proof: if the orphan claim were NOT rolled back,
        the guard's second ``get_or_create`` would return ``created=False``
        and the retry would wrongly suppress forever.
        """
        backend = _FakeBackend()

        def _real_claim(*, mr_url: str, target: GuardTarget) -> GuardDecision:
            _, created = ReviewRequestPost.objects.get_or_create(
                mr_url=mr_url,
                defaults={"slack_channel_id": target.channel_id, "slack_thread_ts": ""},
            )
            return (
                GuardDecision(action="post") if created else GuardDecision(action="suppress", reason="already_claimed")
            )

        # 1st call: no approval → refuse + rollback.
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", side_effect=_real_claim),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code1, _ = _run()
        assert code1 == 2
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

        # 2nd call: approval recorded → must POST, not suppress on a stale claim.
        OnBehalfApproval.record(
            target="https://gitlab.com/org/repo/-/merge_requests/385",
            action="review_request_post",
            approver_id="souliane",
        )
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", side_effect=_real_claim),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code2, payload2 = _run("--title", "fix(scope): thing")
        assert code2 == 0, payload2
        assert payload2["action"] == "post"
        assert len(backend.posts) == 1


class TestReviewRequestPostAgentDisabled(_DataDirMixin, TestCase):
    """``review_request_post_disabled`` refuses the auto-post end-to-end (#2579).

    The customer-overlay scenario: the autonomy collapse has set
    ``on_behalf_post_mode = immediate`` (which would otherwise auto-post a review
    request with no approval), but the overlay runs the ``notify`` tier, which
    resolves ``review_request_post_disabled = True``. The command must refuse with
    no post — the agent stops at "MR is mergeable + review-requestable".
    """

    def _immediate_with_disable(self, *, disabled: bool) -> AbstractContextManager[object]:
        return patch(
            "teatree.on_behalf_gate.get_effective_settings",
            return_value=UserSettings(
                on_behalf_post_mode=OnBehalfPostMode.IMMEDIATE,
                review_request_post_disabled=disabled,
            ),
        )

    def test_disabled_refuses_auto_post_under_immediate(self) -> None:
        backend = _FakeBackend()

        def _real_claim(*, mr_url: str, target: GuardTarget) -> GuardDecision:
            ReviewRequestPost.objects.create(mr_url=mr_url, slack_channel_id=target.channel_id, slack_thread_ts="")
            return GuardDecision(action="post")

        with (
            self._immediate_with_disable(disabled=True),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", side_effect=_real_claim),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--title", "fix(scope): thing")

        assert code == 2, payload
        assert payload["action"] == "refused"
        assert payload["reason"] == "on_behalf_not_approved"
        assert backend.posts == []
        # The orphan claim is rolled back exactly as the missing-approval path.
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

    def test_not_disabled_auto_posts_under_immediate(self) -> None:
        # The control: WITHOUT the disable, ``immediate`` auto-posts (no
        # recorded approval needed). This pins the disable as the only thing
        # that changes the outcome — the test above is anti-vacuous.
        backend = _FakeBackend()
        with (
            self._immediate_with_disable(disabled=False),
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", return_value=GuardDecision(action="post")),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--title", "fix(scope): thing")

        assert code == 0, payload
        assert payload["action"] == "post"
        assert len(backend.posts) == 1


class TestReviewRequestPostHappyPath(_DataDirMixin, TestCase):
    def test_records_consumes_audits_and_persists(self) -> None:
        OnBehalfApproval.record(
            target=_MR_URL,
            action="review_request_post",
            approver_id="souliane",
        )
        backend = _FakeBackend()
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(
                f"{_CMD}.should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run("--title", "fix(scope): thing")

        assert code == 0
        assert payload["action"] == "post"
        assert payload["mr_url"] == _MR_URL
        # exactly one post, "<title> <MR_URL>" message format
        assert len(backend.posts) == 1
        assert backend.posts[0]["text"] == f"fix(scope): thing {_MR_URL}"
        assert backend.posts[0]["channel"] == "C_REVIEW"
        # approval consumed, one audit
        approval = OnBehalfApproval.objects.get()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.count() == 1
        # permalink persisted to mr_review_messages.json with the schema
        cache = self._tmp / "tickets" / "385" / "mr_review_messages.json"
        data = json.loads(cache.read_text())
        assert _MR_URL in data
        assert data[_MR_URL]["channel"] == "C_REVIEW"
        assert data[_MR_URL]["permalink"].startswith("https://team.slack.com/archives/C_REVIEW/")

    def test_second_call_with_consumed_approval_refuses(self) -> None:
        OnBehalfApproval.record(
            target=_MR_URL,
            action="review_request_post",
            approver_id="souliane",
        )
        backend = _FakeBackend()
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(
                f"{_CMD}.should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code1, _ = _run("--title", "t")
            code2, payload2 = _run("--title", "t")

        assert code1 == 0
        assert code2 == 2
        assert payload2["action"] == "refused"
        assert OnBehalfAudit.objects.count() == 1

    def test_default_title_used_when_title_omitted(self) -> None:
        OnBehalfApproval.record(
            target=_MR_URL,
            action="review_request_post",
            approver_id="souliane",
        )
        backend = _FakeBackend()
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(
                f"{_CMD}.should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = _run()  # no --title

        assert code == 0
        assert payload["action"] == "post"
        assert backend.posts[0]["text"] == f"Please review {_MR_URL}"

    def test_iid_falls_back_to_last_segment_for_non_numeric_url(self) -> None:
        non_numeric = "https://github.com/org/repo/pull/feature-branch"
        OnBehalfApproval.record(
            target=non_numeric,
            action="review_request_post",
            approver_id="souliane",
        )
        backend = _FakeBackend()
        buf = io.StringIO()
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(
                f"{_CMD}.should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
            contextlib.redirect_stdout(buf),
            pytest.raises(SystemExit),
        ):
            call_command(
                "review_request_post",
                "--mr-url",
                non_numeric,
                "--approver",
                "souliane",
                "--title",
                "t",
            )

        cache = self._tmp / "tickets" / "feature-branch" / "mr_review_messages.json"
        data = json.loads(cache.read_text())
        assert non_numeric in data

    def test_no_messaging_backend_suppresses_without_post(self) -> None:
        OnBehalfApproval.record(
            target=_MR_URL,
            action="review_request_post",
            approver_id="souliane",
        )
        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(
                f"{_CMD}.should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
            patch(f"{_CMD}.messaging_from_overlay", return_value=None),
        ):
            code, payload = _run("--title", "t")

        assert code == 0
        assert payload["action"] == "suppress"
        assert payload["reason"] == "no_messaging_backend"
        # No backend → no post → the approval is NOT consumed (#1879). The
        # non-consuming peek lets a real post later reuse it; nothing is burned
        # and no audit lies about a post that never happened.
        assert OnBehalfAudit.objects.count() == 0
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=False).count() == 0


class TestReviewRequestPostFinalizesClaim(_DataDirMixin, TestCase):
    """A successful post must finalize the guard's claim row (#1508).

    ``should_post_review_request`` takes the ``ReviewRequestPost``
    ``get_or_create`` claim before the post (``slack_thread_ts=""``,
    ``done_at`` unset). If the command never stamps the thread ts after a
    successful post, the row keeps the *unposted-orphan* shape
    ``_claim_or_reclaim`` reclaims once older than ``_CLAIM_RACE_WINDOW``
    — a later re-attempt posts a duplicate to the review channel (the
    #1084 incident class). After a successful post the row must carry the
    posted thread ts so it can never be reclaimed as an orphan.
    """

    def _post_with_real_claim(self) -> _FakeBackend:
        """Run the happy path with the guard's *real* ``get_or_create`` claim."""
        OnBehalfApproval.record(
            target=_MR_URL,
            action="review_request_post",
            approver_id="souliane",
        )
        backend = _FakeBackend()

        def _real_claim(*, mr_url: str, target: GuardTarget) -> GuardDecision:
            ReviewRequestPost.objects.get_or_create(
                mr_url=mr_url,
                defaults={"slack_channel_id": target.channel_id, "slack_thread_ts": ""},
            )
            return GuardDecision(action="post")

        with (
            patch(f"{_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CMD}.should_post_review_request", side_effect=_real_claim),
            patch(f"{_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = self._run_or_fail()
        assert code == 0, payload
        assert payload["action"] == "post"
        return backend

    @staticmethod
    def _run_or_fail() -> tuple[int, dict[str, object]]:
        return _run("--title", "fix(scope): thing")

    def test_post_stamps_thread_ts_on_claim_row(self) -> None:
        backend = self._post_with_real_claim()
        ts = backend.posts and "1.23"
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        # The posted thread ts is recorded — the backend returned ts="1.23".
        assert post.slack_thread_ts == ts

    def test_post_row_no_longer_matches_orphan_reclaim_predicate(self) -> None:
        self._post_with_real_claim()
        post = ReviewRequestPost.objects.get(mr_url=_MR_URL)
        # ``_claim_or_reclaim`` reclaims when this predicate holds (and the
        # row is stale). A finalized post must break it so no re-attempt
        # can reclaim the row and post a duplicate.
        is_unposted_orphan = post.done_at is None and not post.slack_thread_ts
        assert not is_unposted_orphan
