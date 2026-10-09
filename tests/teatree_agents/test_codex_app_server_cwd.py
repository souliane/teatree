"""A Codex dispatch inherits the ticket's worktree as cwd and runtime workspace root (#5151)."""

import asyncio
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from django.test import TestCase

from teatree.agents import codex_app_server_options
from teatree.agents._runner_options import _build_options
from teatree.agents.codex_app_server import CodexAppServerSession, _thread_params
from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.codex_approval_gate import approval_decision
from teatree.core.models import Session, Task, Worktree
from tests.factories import planned_ticket


class TestCodexStartsInTheWorktree(TestCase):
    def setUp(self) -> None:
        self.checkout = self.enterContext(tempfile.TemporaryDirectory())
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))
        Worktree.objects.create(
            ticket=ticket, overlay="t3-teatree", repo_path="org/repo", extra={"worktree_path": self.checkout}
        )
        self.enterContext(patch.object(codex_app_server_options, "container_is_the_sandbox", return_value=True))
        self.options = CodexAppServerOptions.from_sdk_options(
            _build_options(task, "context", phase="coding", skills=[])
        )

    def test_thread_and_turn_params_carry_the_checkout(self) -> None:
        sent: dict[str, Any] = {}

        async def record(_self: object, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(0)
            sent[method] = params
            return {"turn": {"id": "turn-1"}}

        session = CodexAppServerSession(self.options, resume=None, code_home=Path(self.checkout))
        with patch.object(CodexAppServerSession, "_request", record):
            asyncio.run(session.query("work"))

        thread = _thread_params(self.options)
        for params in (thread, sent["turn/start"]):
            assert params["cwd"] == self.checkout
            assert params["runtimeWorkspaceRoots"] == [self.checkout]

    def test_a_file_change_approval_without_its_own_cwd_is_accepted(self) -> None:
        seen: list[str] = []

        async def accept(_tool: str, _tool_input: object, cwd: str, _params: object) -> tuple[str, None]:
            await asyncio.sleep(0)
            seen.append(cwd)
            return "accept", None

        change = [{"path": "src/x.py", "kind": {"type": "update"}, "diff": "+x"}]
        with patch("teatree.agents.codex_approval_gate._run_router", accept):
            decision = asyncio.run(
                approval_decision("item/fileChange/requestApproval", {"threadId": "t"}, self.options, change)
            )

        assert decision == ("accept", None)
        assert seen == [self.checkout]
