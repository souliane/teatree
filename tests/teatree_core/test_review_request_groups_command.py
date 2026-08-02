"""``review_request_groups`` — the read-only R1 work-group status surface.

Backs ``t3 review-request group-status``. The two properties worth pinning are
that it NEVER reaches a posting surface, and that an unreadable forge exits
non-zero rather than rendering an empty, reassuring survey.
"""

import contextlib
import io
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import DraftState
from teatree.types import RawAPIDict

_GATE = "teatree.core.gates.review_request_batch_gate"
_DRAFT_PROBE_FACTORY = "teatree.core.backend_factory.code_host_from_overlay"
_TICKET = "org/tracker#42"


def _url(number: int) -> str:
    return f"https://gitlab.com/org/repo/-/merge_requests/{number}"


def _mr(number: int, title: str, *, ci: str = "success") -> RawAPIDict:
    return {"web_url": _url(number), "title": title, "head_pipeline": {"status": ci}}


class _Host:
    def __init__(self, *, mrs: list[RawAPIDict], drafts: dict[int, DraftState] | None = None) -> None:
        self.mrs = mrs
        self.drafts = drafts or {}

    def current_user(self) -> str:
        return "souliane"

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        del author, updated_after
        return list(self.mrs)

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        del slug
        return self.drafts.get(pr_id, DraftState.NOT_DRAFT)


_NEVER_POST = AssertionError("the group-status surface must never post")


class _NeverPosts:
    """A messaging backend whose every write is a tripwire."""

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        del channel, text, thread_ts
        raise _NEVER_POST

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        del channel
        return {"ts": ts, "reactions": []}


@contextmanager
def _forge(host: _Host | None) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_GATE}.code_host_from_overlay", return_value=host))
        stack.enter_context(patch(_DRAFT_PROBE_FACTORY, return_value=host))
        stack.enter_context(patch(f"{_GATE}.messaging_from_overlay", return_value=_NeverPosts()))
        yield


def _run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            call_command("review_request_groups", *args)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _two_groups() -> _Host:
    return _Host(
        mrs=[
            _mr(1, f"feat(billing): add the sweep ({_TICKET})"),
            _mr(2, f"feat(billing): wire the sweep ({_TICKET})"),
            _mr(9, "fix(payouts): correct the rounding", ci="running"),
        ]
    )


class TestGroupStatusReports(TestCase):
    def test_a_ready_group_prints_its_ordered_broadcast_lines(self) -> None:
        with _forge(_two_groups()):
            code, out, _ = _run()

        assert code == 0, out
        assert f"work group {_url(1)} — READY (2 members)" in out
        assert out.index(f"post --mr-url {_url(1)}") < out.index(f"post --mr-url {_url(2)}")

    def test_a_held_group_names_the_axis_and_offers_no_broadcast_line(self) -> None:
        with _forge(_two_groups()):
            code, out, _ = _run()

        assert code == 0, out
        assert f"work group {_url(9)} — HOLDING (1 members)" in out
        assert f"HOLDING {_url(9)} — ci_pending" in out
        assert f"post --mr-url {_url(9)}" not in out

    def test_mr_url_narrows_the_report_to_that_one_group(self) -> None:
        with _forge(_two_groups()):
            code, out, _ = _run("--mr-url", _url(9))

        assert code == 0, out
        assert _url(9) in out
        assert f"work group {_url(1)}" not in out

    def test_a_merge_request_outside_the_listing_is_reported_plainly(self) -> None:
        with _forge(_two_groups()):
            code, out, _ = _run("--mr-url", _url(404))

        assert code == 0, out
        assert f"no open work group holds {_url(404)}." in out

    def test_a_drafted_sibling_is_named_on_every_member_line(self) -> None:
        host = _Host(
            mrs=[
                _mr(1, f"feat(billing): add the sweep ({_TICKET})"),
                _mr(2, f"feat(billing): wire the sweep ({_TICKET})"),
            ],
            drafts={2: DraftState.DRAFT},
        )
        with _forge(host):
            code, out, _ = _run()

        assert code == 0, out
        assert f"HOLDING {_url(2)} — draft" in out
        assert f"READY   {_url(1)}" in out


class TestAnUnreadableForgeIsLoud(TestCase):
    """A failed listing must not render as a reassuringly empty survey."""

    def test_no_code_host_exits_non_zero_and_says_why(self) -> None:
        with _forge(None):
            code, out, err = _run()

        assert code == 1
        assert "work groups unavailable" in err
        assert out.strip() == ""

    def test_control_a_readable_forge_exits_zero(self) -> None:
        with _forge(_two_groups()):
            code, _, err = _run()

        assert code == 0
        assert "work groups unavailable" not in err

    def test_a_genuinely_empty_queue_says_so_at_exit_zero(self) -> None:
        with _forge(_Host(mrs=[])):
            code, out, _ = _run()

        assert code == 0
        assert "no open merge requests" in out
