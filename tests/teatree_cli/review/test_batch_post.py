"""N anchored review comments in ONE publish envelope.

``/t3:review`` mandates one inline comment per finding, and each live one cost two
gated ``t3`` processes plus its own single-use authorization — so six findings on one
MR meant six authorizations and twelve ~32s process starts. The batch keeps every gate
(each body is scanned on its own) and shares exactly one authorization consume and one
publish envelope across the findings.
"""

import logging
from collections.abc import Callable

import pytest
from django.test import TestCase

from teatree.cli.review import batch_post, default_draft
from teatree.cli.review.batch_post import InlineNote, post_comments
from teatree.cli.review.evidence_gate import FindingEvidence
from teatree.cli.review.service import ReviewService
from teatree.core import on_behalf_gate_recorded as gate_recorded
from teatree.core.models import LivePostApproval, OnBehalfApproval, OnBehalfAudit
from tests.teatree_cli.review.conftest import OutboundHttpBan
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture, seed_permitting_posture

_APPROVER = "human-operator"
_GATE_LOGGER = "teatree.core.on_behalf_gate_recorded"
_REPO = "org/repo"
_MR = 7


def _raiser(error: Exception) -> Callable[..., None]:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise error

    return _raise


_NOTES = (
    InlineNote(note="This branch drops the retry, so a flaky read now fails the run.", file="src/a.py", line=12),
    InlineNote(note="The token is compared with ==, which is not constant-time here.", file="src/b.py", line=30),
)


class _Posted:
    """A review service whose per-comment publish records the call and succeeds."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str, int]] = []

    def __call__(self, repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
        del repo, mr
        self.posted.append((note, file, line))
        return f"OK note_id={len(self.posted)}", 0


class _BatchCase(TestCase):
    @pytest.fixture(autouse=True)
    def _env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        no_outbound_http: OutboundHttpBan,
        forge_reads_stubbed: None,
    ) -> None:
        del forge_reads_stubbed
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch
        self.outbound = no_outbound_http
        self.service = ReviewService(token="t")
        self.publish = _Posted()
        monkeypatch.setattr(self.service, "_post_comment_impl", self.publish)
        monkeypatch.setattr(batch_post, "route_forge_send", lambda *, repo, mr, action, note: (note, ""))


class TestOneAuthorizationCoversTheWholeBatch(_BatchCase):
    @pytest.fixture(autouse=True)
    def _forbidding_posture(self) -> None:
        seed_forbidding_posture()

    def test_a_single_authorization_publishes_every_comment(self) -> None:
        OnBehalfApproval.record(f"{_REPO}!{_MR}", "post_comment", _APPROVER)
        LivePostApproval.record(mr_url=f"{_REPO}!{_MR}", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 0, message
        assert [(file, line) for _note, file, line in self.publish.posted] == [("src/a.py", 12), ("src/b.py", 30)]
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=False).count() == 1
        assert LivePostApproval.objects.filter(consumed_at__isnull=False).count() == 1
        assert OnBehalfAudit.objects.count() == 1
        assert self.outbound.attempts == 0

    def test_no_authorization_refuses_before_any_comment_is_posted(self) -> None:
        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert "not authorized" in message
        assert self.publish.posted == []


class TestEveryBodyIsGatedOnItsOwn(_BatchCase):
    @pytest.fixture(autouse=True)
    def _permitting_posture(self) -> None:
        seed_permitting_posture()

    def test_one_refused_body_refuses_the_batch_and_posts_nothing(self) -> None:
        chatter = "@alice please sync with the team in standup about this."
        notes = (*_NOTES, InlineNote(note=chatter, file="src/c.py", line=4))

        message, code = post_comments(self.service, _REPO, _MR, notes, live=True)

        assert code == 1
        assert "comment 3" in message
        assert self.publish.posted == []

    def test_a_proceed_verdict_needs_no_live_token(self) -> None:
        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 0, message
        assert len(self.publish.posted) == len(_NOTES)
        assert LivePostApproval.objects.count() == 0


class TestAFailedCommentStopsTheBatchAndSaysWhatLanded(_BatchCase):
    @pytest.fixture(autouse=True)
    def _permitting_posture(self) -> None:
        seed_permitting_posture()

    def test_the_report_names_the_landed_comments_and_the_failure(self) -> None:
        def flaky(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note
            if file == "src/b.py":
                return "Failed to post comment", 1
            self.landed.append((file, line))
            return "OK note_id=1", 0

        self.landed: list[tuple[str, int]] = []
        self.monkeypatch.setattr(self.service, "_post_comment_impl", flaky)

        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert "Failed to post comment" in message
        assert "1 of 2" in message
        assert self.landed == [("src/a.py", 12)]


class TestAPartialBatchKeepsTheAuditForWhatLanded(_BatchCase):
    """A failed batch and an unposted batch are different facts.

    Forge posts are not transactional, so comment 1 stays readable by colleagues under
    the user's identity whatever happens to comment 2 — and the audit row is the only
    record that it went out.

    Rolling the approval back is right (the authorization did not fully deliver, so it
    survives the retry). Rolling the AUDIT back with it is not: it leaves a published
    on-behalf post with no audit trail, and a retry that audits one comment where two
    were posted. The single-comment path cannot reach this state; batching introduces it.
    """

    @pytest.fixture(autouse=True)
    def _forbidding_posture(self) -> None:
        seed_forbidding_posture()

    def _authorize(self) -> None:
        OnBehalfApproval.record(f"{_REPO}!{_MR}", "post_comment", _APPROVER)
        LivePostApproval.record(mr_url=f"{_REPO}!{_MR}", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

    def _fail_on(self, failing_file: str) -> None:
        self.landed: list[tuple[str, int]] = []

        def flaky(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note
            if file == failing_file:
                return "Failed to post comment", 1
            self.landed.append((file, line))
            return "OK note_id=1", 0

        self.monkeypatch.setattr(self.service, "_post_comment_impl", flaky)

    def test_a_comment_that_landed_is_audited_though_the_approval_rolls_back(self) -> None:
        self._authorize()
        self._fail_on("src/b.py")

        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert "1 of 2" in message
        assert self.landed == [("src/a.py", 12)]
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1
        assert LivePostApproval.objects.filter(consumed_at__isnull=True).count() == 1
        assert OnBehalfAudit.objects.count() == 1

    def test_a_batch_that_lands_nothing_writes_no_audit(self) -> None:
        self._authorize()
        self._fail_on("src/a.py")

        _message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert self.landed == []
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1
        assert OnBehalfAudit.objects.count() == 0


class TestEachFindingCarriesItsOwnEvidence(_BatchCase):
    """A finding's receipts belong to the finding, not to the batch it rides in.

    The #1280 gate refuses a "X is wrong/broken/missing" body without receipts, and the
    batch hardcoded ``evidence=None`` — so the finding class a review most needs to post
    was the one class the batch could not, sending it back to the CLI it replaces.
    """

    @pytest.fixture(autouse=True)
    def _permitting_posture(self) -> None:
        seed_permitting_posture()

    _CLAIM = "HIGH (correctness): the retry is missing from the new path, so a flaky read fails the run."
    _RECEIPTS = FindingEvidence(master_check_paths=["src/a.py:12"], confidence="verified")

    def test_a_claim_body_without_evidence_is_refused(self) -> None:
        notes = (InlineNote(note=self._CLAIM, file="src/a.py", line=12),)

        message, code = post_comments(self.service, _REPO, _MR, notes, live=True)

        assert code == 1
        assert "comment 1" in message
        assert self.publish.posted == []

    def test_the_same_body_posts_once_it_carries_its_evidence(self) -> None:
        notes = (InlineNote(note=self._CLAIM, file="src/a.py", line=12, evidence=self._RECEIPTS),)

        message, code = post_comments(self.service, _REPO, _MR, notes, live=True)

        assert code == 0, message
        assert [(file, line) for _note, file, line in self.publish.posted] == [("src/a.py", 12)]

    def test_evidence_is_per_comment_so_a_neighbouring_nit_needs_none(self) -> None:
        notes = (
            InlineNote(note=self._CLAIM, file="src/a.py", line=12, evidence=self._RECEIPTS),
            InlineNote(note="Nit: rename this helper.", file="src/b.py", line=30),
        )

        message, code = post_comments(self.service, _REPO, _MR, notes, live=True)

        assert code == 0, message
        assert len(self.publish.posted) == 2


class TestTheDraftBatchStaysUngated(_BatchCase):
    @pytest.fixture(autouse=True)
    def _forbidding_posture(self, _env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_forbidding_posture()
        self.drafted: list[tuple[str, int]] = []

        def draft(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note
            self.drafted.append((file, line))
            return "OK draft_note_id=1", 0

        monkeypatch.setattr(self.service, "_post_draft_note_impl", draft)
        monkeypatch.setattr(default_draft, "notify_draft_created", lambda **_kwargs: None)
        monkeypatch.setattr(default_draft, "resolve_reviewed_head_sha", lambda *_args: "abc")

    def test_drafts_publish_with_no_approval_recorded(self) -> None:
        message, code = post_comments(self.service, _REPO, _MR, _NOTES)

        assert code == 0, message
        assert self.drafted == [("src/a.py", 12), ("src/b.py", 30)]
        assert OnBehalfApproval.objects.count() == 0


class TestAnEmptyBatchIsRefused(_BatchCase):
    def test_no_comments_is_a_refusal_not_a_silent_success(self) -> None:
        message, code = post_comments(self.service, _REPO, _MR, ())

        assert code == 1
        assert "no comments" in message


class TestNothingAfterThePostsLandCanEraseTheirAudit(_BatchCase):
    """Two ways the audit for a landed comment was still lost, both after the post went out.

    The envelope catches the partial-publish raise, audits what landed outside the
    rolled-back block, and re-raises. If that out-of-band audit itself fails, its
    exception REPLACES the partial-publish signal -- and `_surface` maps only four
    types, so an unmapped one reaches the caller as a crash with the landed comments
    unaudited and nothing saying which ones they were.

    And only a nonzero RETURN from the per-comment publish became the partial-publish
    raise. A publish that raised on its own -- a transport error, a bug -- unwound the
    atomic with no partial signal at all, so the consume and the audit rolled back
    together: the state the partial-publish path exists to prevent, reached by the
    other door.
    """

    @pytest.fixture(autouse=True)
    def _forbidding_posture(self) -> None:
        seed_forbidding_posture()

    def _authorize(self) -> None:
        OnBehalfApproval.record(f"{_REPO}!{_MR}", "post_comment", _APPROVER)
        LivePostApproval.record(mr_url=f"{_REPO}!{_MR}", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

    def _raise_on(self, failing_file: str, error: BaseException) -> None:
        self.landed: list[tuple[str, int]] = []

        def flaky(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note
            if file == failing_file:
                raise error
            self.landed.append((file, line))
            return "OK note_id=1", 0

        self.monkeypatch.setattr(self.service, "_post_comment_impl", flaky)

    def test_a_failing_audit_does_not_replace_the_partial_publish_report(self) -> None:
        self._authorize()

        def flaky(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note, line
            return ("Failed to post comment", 1) if file == "src/b.py" else ("OK note_id=1", 0)

        self.monkeypatch.setattr(self.service, "_post_comment_impl", flaky)
        self.monkeypatch.setattr(
            gate_recorded,
            "_audit_the_posts_that_landed",
            _raiser(RuntimeError("the audit write failed")),
        )

        with self.assertLogs(_GATE_LOGGER, level=logging.ERROR) as captured:
            message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert "1 of 2" in message
        assert len(captured.records) == 1
        record = captured.records[0]
        assert record.levelno == logging.ERROR
        assert "could not audit the posts that landed" in record.getMessage()
        assert record.exc_info is not None, "logger.error would satisfy the message but lose the traceback"
        assert OnBehalfAudit.objects.count() == 0

    def test_a_publish_that_raises_after_one_landed_keeps_that_comments_audit(self) -> None:
        self._authorize()
        self._raise_on("src/b.py", RuntimeError("the forge connection dropped"))

        with self.assertNoLogs(_GATE_LOGGER, level=logging.ERROR):
            message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert "1 of 2" in message
        assert self.landed == [("src/a.py", 12)]
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1
        assert OnBehalfAudit.objects.count() == 1

    def test_a_publish_that_raises_before_anything_landed_propagates_unchanged(self) -> None:
        self._authorize()
        self._raise_on("src/a.py", RuntimeError("the forge connection dropped"))

        with pytest.raises(RuntimeError, match="the forge connection dropped"):
            post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert self.landed == []
        assert OnBehalfAudit.objects.count() == 0


class TestAnInterruptStillTerminatesButTheAuditSurvivesIt(_BatchCase):
    """A `KeyboardInterrupt` mid-batch must audit what landed AND still terminate.

    Only `Exception` reached the partial-publish raise, so an interrupt or a `SystemExit`
    after a comment had gone out unwound the atomic with no partial signal at all -- the
    consume and the audit rolled back together, leaving a colleague-visible post under the
    user's identity with no record of it. Widening the arm is not enough on its own: a
    `KeyboardInterrupt` converted into `OnBehalfPartialPublishError` is mapped by
    `_surface` to `(message, 1)`, so the operator's Ctrl-C returns a report instead of
    stopping the process. The audit runs, then the original cause is re-raised.
    """

    @pytest.fixture(autouse=True)
    def _forbidding_posture(self) -> None:
        seed_forbidding_posture()

    def _authorize(self) -> None:
        OnBehalfApproval.record(f"{_REPO}!{_MR}", "post_comment", _APPROVER)
        LivePostApproval.record(mr_url=f"{_REPO}!{_MR}", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

    def _raise_on(self, failing_file: str, error: BaseException) -> None:
        self.landed: list[tuple[str, int]] = []

        def flaky(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note
            if file == failing_file:
                raise error
            self.landed.append((file, line))
            return "OK note_id=1", 0

        self.monkeypatch.setattr(self.service, "_post_comment_impl", flaky)

    def test_an_interrupt_after_one_landed_audits_it_and_still_interrupts(self) -> None:
        self._authorize()
        self._raise_on("src/b.py", KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert self.landed == [("src/a.py", 12)]
        assert OnBehalfAudit.objects.count() == 1
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1

    def test_a_system_exit_after_one_landed_audits_it_and_still_exits(self) -> None:
        self._authorize()
        self._raise_on("src/b.py", SystemExit(3))

        with pytest.raises(SystemExit):
            post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert self.landed == [("src/a.py", 12)]
        assert OnBehalfAudit.objects.count() == 1

    def test_an_interrupt_before_anything_landed_audits_nothing(self) -> None:
        self._authorize()
        self._raise_on("src/a.py", KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert self.landed == []
        assert OnBehalfAudit.objects.count() == 0


class TestThePartialReportNamesTheRemainingSlice(_BatchCase):
    """A retry has to know WHICH comments are still owed, not just how many landed.

    There is no idempotency key on the batch, so a caller that retries the whole list
    re-posts what already went out. The refusal already knows the landed count, so it
    can name the exact remaining slice -- the cheapest thing that makes the retry
    correct without a new persisted record.
    """

    @pytest.fixture(autouse=True)
    def _permitting_posture(self) -> None:
        seed_permitting_posture()

    def test_the_refusal_names_the_slice_still_owed(self) -> None:
        def flaky(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note, line
            return ("Failed to post comment", 1) if file == "src/b.py" else ("OK note_id=1", 0)

        self.monkeypatch.setattr(self.service, "_post_comment_impl", flaky)

        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert "comments[1:]" in message

    def test_a_batch_that_lands_everything_carries_no_slice_hint(self) -> None:
        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 0
        assert "comments[" not in message

    def test_a_batch_that_lands_nothing_names_no_slice_to_skip(self) -> None:
        def refuse(repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
            del repo, mr, note, file, line
            return "Failed to post comment", 1

        self.monkeypatch.setattr(self.service, "_post_comment_impl", refuse)

        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 1
        assert "comments[" not in message


class TestTheBodyPublishedIsTheROUTEDOne(_BatchCase):
    """The send-proxy's transform must survive the batch, and only a transform can prove it.

    ``route_forge_send`` returns the body to post — REDACTED under ``enforce`` mode — so
    publishing the un-routed note discards the redaction and sends a colleague the very
    text the seam rewrote. Every other test here stubs the proxy as the identity, which
    makes ``routed`` and ``notes`` the same list: swapping one for the other changes
    nothing observable, so the whole suite passed byte-identically with the transform
    dropped. A stub that actually REWRITES is what makes the assertion lethal.
    """

    _REDACTED = "[redacted by the send proxy]"

    @pytest.fixture(autouse=True)
    def _redacting_proxy(self) -> None:
        seed_permitting_posture()
        self.monkeypatch.setattr(
            batch_post,
            "route_forge_send",
            lambda *, repo, mr, action, note: (f"{self._REDACTED} {note}", ""),
        )

    def test_every_published_body_carries_the_proxys_rewrite(self) -> None:
        message, code = post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        assert code == 0, message
        published = [note for note, _file, _line in self.publish.posted]
        assert published == [f"{self._REDACTED} {item.note}" for item in _NOTES]

    def test_no_published_body_is_the_unrouted_original(self) -> None:
        post_comments(self.service, _REPO, _MR, _NOTES, live=True)

        originals = {item.note for item in _NOTES}
        assert not originals & {note for note, _file, _line in self.publish.posted}
