"""Host-facing MR test-plan poster (F3.1).

Direct coverage of :func:`post_mr_test_plan_comment`'s peek-first invariant: the
non-consuming on-behalf block check fires BEFORE any host call, so a blocked post
touches no upload / list / comment API. Exercised on the directly-imported
symbol so a revert of the early peek turns this red.
"""

from collections.abc import Callable
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from teatree.core.management.commands._test_plan.mr_post import MrTestPlanPost, post_mr_test_plan_comment
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError

_MODULE = "teatree.core.management.commands._test_plan.mr_post"


def _passthrough_publish(*, target: str, action: str, publish: Callable[[], Any]) -> Any:
    return publish()


class TestMrTestPlanPostTarget:
    def test_target_is_repo_bang_iid(self) -> None:
        assert MrTestPlanPost(repo="org/backend", mr_iid=42).target == "org/backend!42"


class TestPostMrTestPlanCommentPeekFirst:
    def test_blocked_target_refuses_before_touching_the_host(self) -> None:
        host = MagicMock()
        post = MrTestPlanPost(repo="org/backend", mr_iid=10, title="Test Plan", body="ok", files=["a.png"])
        lines: list[str] = []

        module = f"{_MODULE}.on_behalf_block_message"
        with patch(module, return_value="on-behalf approval required"), pytest.raises(OnBehalfPostBlockedError):
            post_mr_test_plan_comment(host, post, write_out=lines.append)

        # The peek fired first: no upload, no comment-list, no post/update on the host.
        assert host.method_calls == []
        assert lines == []


class TestPostMrTestPlanCommentPublishes:
    def _patched_gates(self, stack: ExitStack) -> None:
        stack.enter_context(patch(f"{_MODULE}.on_behalf_block_message", return_value=""))
        stack.enter_context(patch(f"{_MODULE}.check_blocked_body_from_config", return_value=None))
        stack.enter_context(patch(f"{_MODULE}.route_forge_write", side_effect=lambda **kw: kw["text"]))
        stack.enter_context(patch(f"{_MODULE}.require_on_behalf_approval", side_effect=_passthrough_publish))
        stack.enter_context(patch(f"{_MODULE}.notify_user_on_behalf_post", return_value=None))

    def test_no_existing_note_creates_and_embeds_marker(self) -> None:
        host = MagicMock()
        host.list_pr_comments.return_value = []
        host.post_pr_comment.return_value = {"id": 501, "web_url": "https://forge/comment/501"}
        verified = MagicMock(ok=True, embed_url="/uploads/abc/shot.png")
        host.verify_upload.return_value = verified
        post = MrTestPlanPost(repo="org/backend", mr_iid=7, title="Test Plan", body="ran it", files=["shot.png"])
        lines: list[str] = []

        with ExitStack() as stack:
            self._patched_gates(stack)
            result = post_mr_test_plan_comment(host, post, write_out=lines.append)

        assert result["id"] == 501
        posted_body = host.post_pr_comment.call_args.kwargs["body"]
        assert "org/backend!7" in posted_body  # hidden idempotency marker scoped to THIS MR
        assert "![shot.png](/uploads/abc/shot.png)" in posted_body
        host.update_pr_comment.assert_not_called()

    def test_existing_note_updates_in_place(self) -> None:
        host = MagicMock()
        marker = "<!-- t3-e2e-evidence ticket=org/backend!7 -->"
        host.list_pr_comments.return_value = [{"id": 88, "body": f"## Test Plan\n\nold\n\n{marker}"}]
        host.update_pr_comment.return_value = {"id": 88}
        post = MrTestPlanPost(repo="org/backend", mr_iid=7, title="Test Plan", body="new", files=[])
        lines: list[str] = []

        with ExitStack() as stack:
            self._patched_gates(stack)
            result = post_mr_test_plan_comment(host, post, write_out=lines.append)

        assert result["id"] == 88
        assert host.update_pr_comment.call_args.kwargs["comment_id"] == 88
        host.post_pr_comment.assert_not_called()
