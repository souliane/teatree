"""Host-facing MR test-plan poster (F3.1).

Direct coverage of :func:`post_mr_test_plan_comment`'s peek-first invariant: the
non-consuming on-behalf block check fires BEFORE any host call, so a blocked post
touches no upload / list / comment API. Exercised on the directly-imported
symbol so a revert of the early peek turns this red.
"""

from unittest.mock import MagicMock, patch

import pytest

from teatree.core.management.commands._test_plan.post import MrTestPlanPost, post_mr_test_plan_comment
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError


class TestPostMrTestPlanCommentPeekFirst:
    def test_blocked_target_refuses_before_touching_the_host(self) -> None:
        host = MagicMock()
        post = MrTestPlanPost(repo="org/backend", mr_iid=10, title="Test Plan", body="ok", files=["a.png"])
        lines: list[str] = []

        module = "teatree.core.management.commands._test_plan.post.on_behalf_block_message"
        with patch(module, return_value="on-behalf approval required"), pytest.raises(OnBehalfPostBlockedError):
            post_mr_test_plan_comment(host, post, write_out=lines.append)

        # The peek fired first: no upload, no comment-list, no post/update on the host.
        assert host.method_calls == []
        assert lines == []
