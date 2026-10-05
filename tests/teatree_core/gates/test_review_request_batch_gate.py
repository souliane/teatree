"""R1 — a ready merge request waits for its whole work group before any broadcast.

Every fail-closed assertion here is paired with a GREEN CONTROL over the same
fixture: a refusal with no control cannot tell "the gate correctly held the
batch" from "my fake answered nothing at all", and both present as a passing
test. Each control changes exactly the one axis under test and asserts the gate
DOES release the group.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.backends.gitlab import GitLabCodeHost
from teatree.core.backend_protocols import DraftState
from teatree.core.gates.review_request_batch_gate import (
    WORK_GROUP_MAX_MEMBERS,
    post_command_lines,
    work_group_batch_refusal,
    work_group_ready,
    work_groups,
)
from teatree.core.gates.review_request_guard import GuardDecision, GuardTarget
from teatree.core.models import (
    ConfigSetting,
    DeferredQuestion,
    OnBehalfApproval,
    ReviewEvidence,
    ReviewRequestPost,
    Ticket,
)
from teatree.types import RawAPIDict
from tests.teatree_backends._gitlab_wire import GitLabWire

_GATE = "teatree.core.gates.review_request_batch_gate"
_DRAFT_PROBE_FACTORY = "teatree.core.backend_factory.code_host_from_overlay"
_POST_CMD = "teatree.core.management.commands.review_request_post"
_CHECK_CMD = "teatree.core.management.commands.review_request_check"

_TICKET = "org/tracker#42"
_TARGET = GuardTarget(channel_id="C_REVIEW", channel_name="the-review-team", token="xoxp")


def _url(number: int, *, slug: str = "org/repo") -> str:
    return f"https://gitlab.com/{slug}/-/merge_requests/{number}"


def _mr(number: int, title: str, *, ci: str = "success", slug: str = "org/repo") -> RawAPIDict:
    return {"web_url": _url(number, slug=slug), "title": title, "head_pipeline": {"status": ci}}


class _Host:
    """The operator's global open-MR listing plus a per-MR draft verdict."""

    def __init__(
        self,
        *,
        mrs: list[RawAPIDict],
        drafts: dict[int, DraftState] | None = None,
        user: str = "souliane",
        listing_error: Exception | None = None,
    ) -> None:
        self.mrs = mrs
        self.drafts = drafts or {}
        self.user = user
        self.listing_error = listing_error

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        del author, updated_after
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.mrs)

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        del slug
        return self.drafts.get(pr_id, DraftState.NOT_DRAFT)


class _Messaging:
    """Slack stand-in for the pause read: one root message, or a transport that errors."""

    def __init__(self, *, message: RawAPIDict | None = None, error: Exception | None = None) -> None:
        self.message = message
        self.error = error

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        del channel, ts
        if self.error is not None:
            raise self.error
        return self.message or {}


class _Backend:
    """Minimal messaging backend for the post path: records the one post, no network."""

    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": "1.23"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://team.slack.com/archives/{channel}/p{ts.replace('.', '')}"


@contextmanager
def _forge(host: _Host, *, messaging: _Messaging | None = None) -> Iterator[None]:
    """Bind *host* to both the batch gate and the draft gate it delegates to."""
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_GATE}.code_host_from_overlay", return_value=host))
        stack.enter_context(patch(_DRAFT_PROBE_FACTORY, return_value=host))
        stack.enter_context(patch(f"{_GATE}.messaging_from_overlay", return_value=messaging))
        yield


def _requested(mr_url: str) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=mr_url,
        slack_channel_id="C_REVIEW",
        slack_thread_ts="1.23",
    )


def _pair_sharing_a_ticket(*, drafts: dict[int, DraftState] | None = None) -> _Host:
    return _Host(
        mrs=[
            _mr(1, f"feat(billing): add the sweep ({_TICKET})"),
            _mr(2, f"feat(billing): wire the sweep ({_TICKET})"),
        ],
        drafts=drafts,
    )


class TestStandaloneMergeRequest(TestCase):
    """R9 must not be stalled by R1: a group of one satisfies the gate trivially."""

    def test_group_of_one_is_ready(self) -> None:
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep")])
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict
        assert verdict.blockers == ()
        assert verdict.reason == ""
        assert verdict.group_key == _url(1)
        assert [member.mr_url for member in verdict.members] == [_url(1)]


class TestDraftStateFailsClosed(TestCase):
    """An unreadable draft state cannot rule a Draft out, so the batch is held."""

    def test_unknown_draft_state_holds_the_group(self) -> None:
        host = _Host(
            mrs=[_mr(1, "feat(billing): add the sweep")],
            drafts={1: DraftState.UNKNOWN},
        )
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.reason == "work_group_member_not_ready"
        assert verdict.blockers == (f"{_url(1)}: draft_state_unknown",)

    def test_control_confirmed_non_draft_releases_the_same_group(self) -> None:
        host = _Host(
            mrs=[_mr(1, "feat(billing): add the sweep")],
            drafts={1: DraftState.NOT_DRAFT},
        )
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict


class TestCiStateFailsClosed(TestCase):
    """A status the pipeline allowlist cannot name is never read as a pass."""

    def test_unknown_ci_holds_the_group(self) -> None:
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep", ci="canceled")])
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.blockers == (f"{_url(1)}: ci_unknown",)

    def test_control_green_ci_releases_the_same_group(self) -> None:
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep", ci="success")])
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict

    def test_a_pipeline_whose_jobs_were_all_skipped_is_settled_and_releases_the_group(self) -> None:
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep", ci="skipped")])
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict

    def test_a_running_pipeline_is_named_rather_than_lumped_into_unknown(self) -> None:
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep", ci="running")])
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.blockers == (f"{_url(1)}: ci_pending",)


class TestPauseStateFailsClosed(TestCase):
    """An unreadable pause is not "not paused" — the owner's hold stays armed."""

    def test_unreadable_pause_holds_the_group(self) -> None:
        _requested(_url(1))
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep")])
        messaging = _Messaging(error=RuntimeError("slack unreachable"))
        with _forge(host, messaging=messaging):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.blockers == (f"{_url(1)}: pause_unknown",)

    def test_control_readable_unpaused_thread_releases_the_same_group(self) -> None:
        _requested(_url(1))
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep")])
        messaging = _Messaging(message={"ts": "1.23", "reactions": []})
        with _forge(host, messaging=messaging):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict

    def test_a_live_pause_reaction_holds_the_group(self) -> None:
        _requested(_url(1))
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep")])
        messaging = _Messaging(message={"ts": "1.23", "reactions": [{"name": "pause_button"}]})
        with _forge(host, messaging=messaging):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.blockers == (f"{_url(1)}: paused",)

    def test_a_resumed_request_is_ready_again(self) -> None:
        post = _requested(_url(1))
        post.resumed_at = timezone.now()
        post.save(update_fields=["resumed_at"])
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep")])
        messaging = _Messaging(message={"ts": "1.23", "reactions": [{"name": "pause_button"}]})
        with _forge(host, messaging=messaging):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict

    def test_a_never_broadcast_merge_request_reads_no_pause_at_all(self) -> None:
        """No ``ReviewRequestPost`` row means no hold exists, so no transport is touched."""
        host = _Host(mrs=[_mr(1, "feat(billing): add the sweep")])
        messaging = _Messaging(error=AssertionError("an unposted merge request has no thread to read"))
        with _forge(host, messaging=messaging):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict


class TestOversizeGroupFailsClosed(TestCase):
    """Past the bound the shared signal is likelier a coincidence than one unit of work."""

    def _group(self, members: int) -> _Host:
        return _Host(mrs=[_mr(n, f"feat(billing): sweep step {n} ({_TICKET})") for n in range(1, members + 1)])

    def test_group_above_the_bound_holds_and_asks_the_owner(self) -> None:
        with _forge(self._group(WORK_GROUP_MAX_MEMBERS + 1)):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.reason == "work_group_too_large"
        assert DeferredQuestion.objects.filter(dedupe_marker=f"mr-state:{_url(1)}").count() == 1

    def test_control_group_within_the_bound_releases_and_asks_nothing(self) -> None:
        with _forge(self._group(WORK_GROUP_MAX_MEMBERS)):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict
        assert DeferredQuestion.objects.count() == 0

    def test_the_read_only_survey_holds_the_oversize_group_without_asking(self) -> None:
        with _forge(self._group(WORK_GROUP_MAX_MEMBERS + 1)):
            surveyed = work_groups()
        assert [verdict.reason for verdict in surveyed] == ["work_group_too_large"]
        assert DeferredQuestion.objects.count() == 0


class TestOneUnreadyMemberHoldsEveryMember(TestCase):
    """The whole point of R1: readiness is a property of the group, not of a member."""

    def test_a_drafted_sibling_holds_all_three(self) -> None:
        host = _Host(
            mrs=[
                _mr(1, f"feat(billing): add the sweep ({_TICKET})"),
                _mr(2, f"feat(billing): wire the sweep ({_TICKET})"),
                _mr(3, f"feat(billing): document the sweep ({_TICKET})"),
            ],
            drafts={2: DraftState.DRAFT},
        )
        for number in (1, 2, 3):
            with _forge(host):
                verdict = work_group_ready(mr_url=_url(number))
            assert not verdict.ready, number
            assert verdict.blockers == (f"{_url(2)}: draft",)
            assert verdict.group_key == _url(1)


class TestReviewExemptMemberIsHonouredBothWays(TestCase):
    """The setting is undecided doctrine, so neither reading may be hard-coded."""

    def _host(self) -> _Host:
        return _Host(
            mrs=[
                _mr(1, f"feat(billing): add the sweep ({_TICKET})"),
                _mr(7, f"feat(billing): bump the chart ({_TICKET})", ci="canceled", slug="devops/charts"),
            ]
        )

    def _declare_exempt(self) -> None:
        ConfigSetting.objects.set_value("review_exempt_repos", ["devops/charts"])

    def test_counting_toward_readiness_holds_the_group(self) -> None:
        self._declare_exempt()
        ConfigSetting.objects.set_value("review_exempt_repos_count_toward_group_readiness", value=True)
        with _forge(self._host()):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.blockers == (f"{_url(7, slug='devops/charts')}: ci_unknown",)

    def test_not_counting_toward_readiness_releases_the_group(self) -> None:
        self._declare_exempt()
        ConfigSetting.objects.set_value("review_exempt_repos_count_toward_group_readiness", value=False)
        with _forge(self._host()):
            verdict = work_group_ready(mr_url=_url(1))
        assert verdict.ready, verdict

    def test_an_exempt_member_is_listed_but_never_offered_a_post_line(self) -> None:
        self._declare_exempt()
        ConfigSetting.objects.set_value("review_exempt_repos_count_toward_group_readiness", value=False)
        with _forge(self._host()):
            verdict = work_group_ready(mr_url=_url(1))
        assert {member.mr_url for member in verdict.members} == {_url(1), _url(7, slug="devops/charts")}
        assert post_command_lines(verdict) == (f"t3 review-request post --mr-url {_url(1)} --approver <your-user-id>",)


class TestUnreadableWorldFailsClosed(TestCase):
    """No host, an erroring listing, or a merge request absent from it are all not-ready."""

    def test_no_code_host_holds_the_group(self) -> None:
        with (
            patch(f"{_GATE}.code_host_from_overlay", return_value=None),
            patch(f"{_GATE}.messaging_from_overlay", return_value=None),
        ):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.reason == "code_host_unavailable"

    def test_an_erroring_listing_holds_rather_than_surveying_an_empty_world(self) -> None:
        host = _Host(mrs=[], listing_error=RuntimeError("forge 502"))
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
            surveyed = work_groups()
        assert verdict.reason == "code_host_unreadable"
        assert [entry.reason for entry in surveyed] == ["code_host_unreadable"]

    def test_a_merge_request_absent_from_the_listing_holds(self) -> None:
        host = _Host(mrs=[_mr(2, "feat(billing): a different unit of work")])
        with _forge(host):
            verdict = work_group_ready(mr_url=_url(1))
        assert not verdict.ready
        assert verdict.reason == "work_group_unresolved"


class TestReadOnlySurvey(TestCase):
    """``work_groups`` renders every group once, ordered, with the broadcast lines."""

    def test_each_group_is_reported_once_with_its_post_lines(self) -> None:
        host = _Host(
            mrs=[
                _mr(1, f"feat(billing): add the sweep ({_TICKET})"),
                _mr(2, f"feat(billing): wire the sweep ({_TICKET})"),
                _mr(9, "fix(payouts): correct the rounding"),
            ]
        )
        with _forge(host):
            surveyed = work_groups()
        assert [verdict.group_key for verdict in surveyed] == [_url(1), _url(9)]
        assert post_command_lines(surveyed[0]) == (
            f"t3 review-request post --mr-url {_url(1)} --approver <your-user-id>",
            f"t3 review-request post --mr-url {_url(2)} --approver <your-user-id>",
        )

    def test_a_held_group_offers_no_post_lines(self) -> None:
        with _forge(_pair_sharing_a_ticket(drafts={2: DraftState.DRAFT})):
            surveyed = work_groups()
        assert post_command_lines(surveyed[0]) == ()


class TestTheGateCanBeDisabled(TestCase):
    """The batch gate always holds an unready group and releases a ready one."""

    def test_arming_it_returns_the_holding_verdict(self) -> None:
        with _forge(_pair_sharing_a_ticket(drafts={2: DraftState.DRAFT})):
            refusal = work_group_batch_refusal(_url(1))
        assert refusal is not None
        assert refusal.blockers == (f"{_url(2)}: draft",)

    def test_arming_it_stays_silent_on_a_ready_group(self) -> None:
        with _forge(_pair_sharing_a_ticket()):
            assert work_group_batch_refusal(_url(1)) is None


class _ChokepointCase(TestCase):
    """Shared wiring for the two commands the gate refuses at."""

    def setUp(self) -> None:
        super().setUp()
        self.ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        self.ticket.record_anti_vacuity_attestation("a" * 40, "ACs checked against diff", [], no_new_tests=True)
        ReviewEvidence.record(
            ticket=self.ticket,
            kind=ReviewEvidence.Kind.COLD_REVIEW,
            reviewer_identity="reviewer-bob",
            verdict="merge_safe",
            head_sha="a" * 40,
        )
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


class TestPostChokepoint(_ChokepointCase):
    """``review_request_post`` refuses a held batch BEFORE the dedup claim."""

    def _run(self, backend: _Backend) -> tuple[int, dict[str, object]]:
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                call_command(
                    "review_request_post",
                    "--mr-url",
                    _url(1),
                    "--approver",
                    "souliane",
                    "--title",
                    "t",
                    "--ticket-id",
                    str(self.ticket.pk),
                    "--head-sha",
                    "a" * 40,
                )
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 1
        del backend
        payload: dict[str, object] = {}
        for raw in buf.getvalue().splitlines():
            line = raw.strip()
            if line.startswith("{"):
                payload = json.loads(line)
        return code, payload

    @contextmanager
    def _post_path(self, backend: _Backend) -> Iterator[None]:
        OnBehalfApproval.record(target=_url(1), action="review_request_post", approver_id="souliane")
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_POST_CMD}.resolve_guard_target", return_value=_TARGET))
            stack.enter_context(
                patch(f"{_POST_CMD}.should_post_review_request", return_value=GuardDecision(action="post"))
            )
            stack.enter_context(patch(f"{_POST_CMD}._owner_authorship", return_value=True))
            stack.enter_context(patch(f"{_POST_CMD}.messaging_from_overlay", return_value=backend))
            yield

    def test_armed_gate_refuses_and_leaves_no_claim_behind(self) -> None:
        backend = _Backend()
        with (
            _forge(_pair_sharing_a_ticket(drafts={2: DraftState.DRAFT})),
            patch(f"{_POST_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(
                f"{_POST_CMD}.should_post_review_request",
                side_effect=AssertionError("the dedup claim must not be taken for a held batch"),
            ),
            patch(f"{_POST_CMD}.messaging_from_overlay", return_value=backend),
        ):
            code, payload = self._run(backend)

        assert code == 2, payload
        assert payload["action"] == "refused"
        assert payload["reason"] == "work_group_not_ready"
        assert payload["blockers"] == [f"{_url(2)}: draft"]
        assert backend.posts == []
        assert ReviewRequestPost.objects.filter(mr_url=_url(1)).count() == 0

    def test_control_an_armed_gate_releases_a_ready_group(self) -> None:
        backend = _Backend()
        with _forge(_pair_sharing_a_ticket()), self._post_path(backend):
            code, payload = self._run(backend)

        assert code == 0, payload
        assert payload["action"] == "post"


class TestCheckChokepoint(_ChokepointCase):
    """``review_request_check`` predicts the same verdict ``post`` would reach."""

    def _run(self) -> dict[str, object]:
        return cast("dict[str, object]", call_command("review_request_check", "--mr-url", _url(1)))

    def test_armed_gate_refuses_and_takes_no_claim(self) -> None:
        with (
            _forge(_pair_sharing_a_ticket(drafts={2: DraftState.DRAFT})),
            patch(f"{_CHECK_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(
                f"{_CHECK_CMD}.peek_should_post_review_request",
                side_effect=AssertionError("a held batch must refuse before the live scan"),
            ),
        ):
            result = self._run()

        assert result["action"] == "refused"
        assert result["reason"] == "work_group_not_ready"
        assert result["blockers"] == [f"{_url(2)}: draft"]
        assert ReviewRequestPost.objects.count() == 0


class TestCheckReadsTheRealGitLabListing(_ChokepointCase):
    """GitLab's global MR listing carries no pipeline field: the gate must see each MR's real pipeline."""

    _HEAD = "a" * 40

    @classmethod
    def _gitlab(cls, pipeline_status: str, *, pipeline_sha: str = _HEAD) -> GitLabCodeHost:
        return GitLabCodeHost(
            client=GitLabWire(
                {
                    "user": {"username": "souliane"},
                    "merge_requests": [
                        {
                            "iid": 1,
                            "project_id": 42,
                            "sha": cls._HEAD,
                            "title": "feat(billing): add the sweep",
                            "web_url": _url(1),
                        },
                    ],
                    "projects/org%2Frepo": {"id": 42, "path_with_namespace": "org/repo", "path": "repo"},
                    "projects/42/merge_requests/1": {"iid": 1, "draft": False},
                    "projects/42/merge_requests/1/pipelines": [
                        {"status": pipeline_status, "sha": pipeline_sha, "web_url": "https://p/1"}
                    ],
                }
            )
        )

    def _check(self, host: GitLabCodeHost) -> dict[str, object]:
        with (
            _forge(host),
            patch(f"{_CHECK_CMD}._owner_authorship", return_value=True),
            patch(f"{_CHECK_CMD}.resolve_guard_target", return_value=_TARGET),
            patch(f"{_CHECK_CMD}.peek_should_post_review_request", return_value=GuardDecision(action="post")),
        ):
            return cast("dict[str, object]", call_command("review_request_check", "--mr-url", _url(1)))

    def test_a_ready_gitlab_mr_is_not_refused_as_ci_unknown(self) -> None:
        result = self._check(self._gitlab("success"))

        assert result["action"] == "post", result

    def test_control_a_running_pipeline_still_holds_the_group(self) -> None:
        result = self._check(self._gitlab("running"))

        assert result["reason"] == "work_group_not_ready", result
        assert result["blockers"] == [f"{_url(1)}: ci_pending"]

    def test_an_older_commits_green_pipeline_does_not_release_the_head(self) -> None:
        result = self._check(self._gitlab("success", pipeline_sha="b" * 40))

        assert result["reason"] == "work_group_not_ready", result
        assert result["blockers"] == [f"{_url(1)}: ci_unknown"]
