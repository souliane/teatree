import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolResultBlock
from pydantic_ai import Agent
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry, UnexpectedModelBehavior
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.function import FunctionToolset

from teatree.agents.harness import PydanticAiHarness, PydanticAiHarnessSession
from teatree.agents.lane_b.gating import (
    _MAX_TRACKED_RUNS,
    HardDenyToolset,
    hard_deny_reason,
    make_soft_gate_predicate,
    raise_if_soft_gated,
)
from teatree.hooks import _repo_visibility
from tests._git_repo import make_git_repo, run_git
from tests.teatree_agents.lane_b._managed_clone import linked_worktree, managed_main_clone

_MUTATIONS = ("git reset --hard HEAD~1", "git checkout my-feature", "git stash pop")
_DENIED_CALL = {"command": "gh pr merge 5"}  # a registry hard-deny (raw forge merge)


class TestHardDenyReason:
    def test_main_clone_mutation_is_denied_in_a_managed_main_clone(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        for command in _MUTATIONS:
            assert hard_deny_reason("Bash", {"command": command}, cwd=clone) is not None

    def test_same_mutation_is_allowed_in_a_linked_worktree(self, tmp_path: Path) -> None:
        # The Lane-B jail root is the WORKTREE, not the main clone — the routine
        # worktree git ops Lane A allows must not be denied here (the fix).
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        for command in _MUTATIONS:
            assert hard_deny_reason("Bash", {"command": command}, cwd=wt) is None

    def test_safe_git_is_allowed_even_in_a_managed_main_clone(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        for command in ("git fetch origin", "git checkout main", "git worktree add ../wt origin/main", "git status"):
            assert hard_deny_reason("Bash", {"command": command}, cwd=clone) is None

    def test_unmanaged_clone_is_not_gated(self, tmp_path: Path) -> None:
        # A repo no overlay owns (a random clone) must never be blocked.
        clone = make_git_repo(tmp_path / "random")
        run_git(clone, "remote", "add", "origin", "git@github.com:randomuser/randomrepo.git")
        assert hard_deny_reason("Bash", {"command": "git checkout feature"}, cwd=clone) is None

    def test_no_cwd_never_denies_a_main_clone_mutation(self, tmp_path: Path) -> None:
        # No jail root → no repo to key off → the main-clone half cannot fire.
        for command in _MUTATIONS:
            assert hard_deny_reason("Bash", {"command": command}) is None

    def test_non_command_tool_with_clean_text_is_allowed(self) -> None:
        assert hard_deny_reason("Read", {"path": "src/app.py"}) is None

    def test_local_write_with_a_high_finding_content_is_allowed(self, tmp_path: Path) -> None:
        # Lane A never scans a local write (extract_publish_payload → None), so
        # Lane B must not either: write_file content is not an egress. RED before
        # the fix, when every string arg of every tool was scanned.
        args = {"path": "note.md", "content": "**User directive (verbatim):** go"}
        assert hard_deny_reason("Write", args, cwd=tmp_path) is None

    def test_non_publish_shell_command_with_a_high_finding_is_allowed(self, tmp_path: Path) -> None:
        # `echo "..." > file` is not a publish — the payload scoping returns None.
        args = {"command": 'echo "**User directive (verbatim):** go" > note.md'}
        assert hard_deny_reason("Bash", args, cwd=tmp_path) is None

    @staticmethod
    def _isolate_visibility(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> None:
        # Hermetic config home + fresh visibility cache, and pin the probe verdict,
        # so the destination gate resolves the target from the monkeypatch alone.
        home = tmp_path / "vishome"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: verdict)

    def test_publish_high_finding_to_a_confirmed_public_target_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A HIGH body to a CONFIRMED-PUBLIC egress is a real public leak → denied,
        # matching Lane A's ``resolve_high_verdict`` (public-egress protection intact).
        self._isolate_visibility(tmp_path, monkeypatch, "PUBLIC")
        args = {"command": 'gh pr comment 5 --repo souliane/teatree --body "**User directive (verbatim):** go"'}
        reason = hard_deny_reason("Bash", args, cwd=tmp_path)
        assert reason is not None
        assert "privacy/banned-term gate" in reason

    def test_publish_high_finding_to_a_probe_error_target_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #3442 fail closed: the target IS a resolvable ``owner/repo`` slug
        # (``someowner/mystery``); only its visibility PROBE fails (None). A target
        # the gate cannot PROVE non-public is scanned, so a HIGH finding is DENIED
        # on both lanes -- a probe error must not route a leak out unscanned. Lane A
        # and Lane B agree via the shared ``gate_skips_for_visibility`` predicate.
        self._isolate_visibility(tmp_path, monkeypatch, None)
        args = {"command": 'gh pr comment 5 --repo someowner/mystery --body "**User directive (verbatim):** go"'}
        reason = hard_deny_reason("Bash", args, cwd=tmp_path)
        assert reason is not None
        assert "privacy/banned-term gate" in reason

    def test_publish_high_finding_to_a_genuinely_unresolvable_target_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #F7.2 fail-closed tightening: an UNRESOLVED ``gh``/``glab`` publish -- no
        # ``--repo`` flag and a cwd with no git remote, so the destination resolves
        # to nothing -- is NOT provably non-public and now FAILS CLOSED to a scan,
        # so a HIGH body is DENIED. (#3442 previously ALLOWED this "genuinely
        # unresolvable" path, but a gate that could not resolve a target does NOT
        # prove no egress -- a ``cd public && gh ...``, a subshell, or a late
        # ``GH_REPO`` could still egress -- so F7.2 deliberately scans every
        # unresolved publish; only a RESOLVED, provably-non-public dest skips.)
        # Lane B mirrors Lane A: this matches
        # ``test_public_visibility.TestUnresolvedStructuredWriteDoesNotSkip``.
        self._isolate_visibility(tmp_path, monkeypatch, None)
        args = {"command": 'gh pr comment 5 --body "**User directive (verbatim):** go"'}
        reason = hard_deny_reason("Bash", args, cwd=tmp_path)
        assert reason is not None
        assert "privacy/banned-term gate" in reason

    def test_publish_high_finding_to_a_private_target_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A HIGH body to a probe-CONFIRMED-PRIVATE repo cannot leak to the public —
        # Lane A downgrades it, so Lane B allows it (no over-deny of a private post).
        self._isolate_visibility(tmp_path, monkeypatch, "PRIVATE")
        args = {"command": 'gh pr comment 5 --repo someowner/private-svc --body "**User directive (verbatim):** go"'}
        assert hard_deny_reason("Bash", args, cwd=tmp_path) is None

    def test_full_gate_parity_with_lane_a_across_clone_and_worktree(self, tmp_path: Path) -> None:
        # Parity against Lane A's FULL gate (the PreToolUse handler = the pure
        # classifier PLUS the environmental main-clone check), not just the core
        # classifier: for every command, in BOTH a managed main clone and a
        # linked worktree, Lane B's hard-deny verdict must equal Lane A's.
        import hooks.scripts.hook_router as router  # noqa: PLC0415

        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        commands = (
            "git reset --hard",
            "git checkout feature-x",
            "git fetch origin",
            "git checkout main",
            "git restore src/a.py",
            "git stash pop",
        )
        for cwd in (clone, wt):
            for command in commands:
                lane_b_denied = hard_deny_reason("Bash", {"command": command}, cwd=cwd) is not None
                event = {
                    "session_id": "parity",
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "cwd": str(cwd),
                }
                lane_a_denied = router.handle_block_main_clone_mutation(event)
                assert lane_b_denied is lane_a_denied, (command, cwd)


class TestHardDenyRetryCap:
    """A hard-deny is a retryable ``ModelRetry`` UNTIL the per-run cap, then terminal.

    A predicate false-positive the model cannot satisfy (or a blocked path it keeps
    re-attempting with variations) would loop, burning tokens; the cap converts the
    N-th refusal in a run into a terminal :class:`UnexpectedModelBehavior` so the
    run ends cleanly instead of looping.
    """

    def test_denials_retry_until_the_cap_then_abort(self) -> None:
        toolset = HardDenyToolset(FunctionToolset(), max_denials=3)
        ctx = SimpleNamespace(run_id="run-1")
        for _ in range(2):
            with pytest.raises(ModelRetry):
                asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, ctx, None))
        with pytest.raises(UnexpectedModelBehavior):
            asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, ctx, None))

    def test_the_cap_is_isolated_per_run(self) -> None:
        # A fresh run_id starts a fresh tally — the cap is per-run, not per-toolset.
        toolset = HardDenyToolset(FunctionToolset(), max_denials=2)
        with pytest.raises(ModelRetry):
            asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, SimpleNamespace(run_id="run-1"), None))
        with pytest.raises(ModelRetry):
            asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, SimpleNamespace(run_id="run-2"), None))


class TestToolFailuresReachTheModelInsteadOfKillingTheRun:
    """A tool failing on the model's OWN input is retryable, not fatal.

    A jailed path escaped ``call_tool`` uncaught, propagated out of ``Agent.run``
    (nothing in the session's ``_RUN_ERRORS`` matches it) and killed the whole
    dispatch with a traceback — one bad path threw away everything a working
    coding run had done, when the model only needed to be told the path was
    outside its worktree. The observed instance:
    ``PathTraversalError: path '<other worktree>/tests/test_x.py' resolves outside
    the worktree root '<jail>'``.
    """

    @staticmethod
    def _reads_then_answers(path: str) -> FunctionModel:
        """Call ``Read`` on *path*, then finish — the shape a coding run opens with."""
        turns = {"n": 0}

        def stream_fn(_messages: object, _info: object) -> object:
            turns["n"] += 1
            turn = turns["n"]

            async def gen():  # noqa: RUF029 — an async generator (the stream contract) that only yields.
                if turn == 1:
                    yield {0: DeltaToolCall(name="Read", json_args=json.dumps({"path": path}), tool_call_id="c1")}
                else:
                    yield "done"

            return gen()

        return FunctionModel(stream_function=stream_fn)

    def _run(self, model: FunctionModel, root: Path) -> list[object]:
        harness = PydanticAiHarness(model=model, phase="coding")

        async def drive() -> list[object]:
            async with harness.open(ClaudeAgentOptions(cwd=str(root))) as session:
                await session.query("implement the ticket")
                return [message async for message in session.receive_response()]

        return asyncio.run(drive())

    @staticmethod
    def _errors(messages: list[object]) -> list[str]:
        return [
            str(block.content)
            for message in messages
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolResultBlock) and block.is_error
        ]

    @pytest.mark.parametrize(
        ("bad_path", "expected"),
        [
            ("/etc/passwd", "resolves outside the worktree root"),
            ("nope.py", "No such file"),
        ],
        ids=["outside-the-jail", "missing-file"],
    )
    def test_a_bad_path_comes_back_as_a_tool_error_and_the_run_finishes(
        self, tmp_path: Path, bad_path: str, expected: str
    ) -> None:
        # RED before the fix: the exception escaped Agent.run and this raised.
        messages = self._run(self._reads_then_answers(bad_path), tmp_path)

        assert any(expected in error for error in self._errors(messages)), self._errors(messages)
        terminal = next(m for m in messages if isinstance(m, ResultMessage))
        assert terminal.is_error is False, "the model was told and corrected — the run must not die"
        assert terminal.result == "done"

    def test_a_readable_file_still_reads(self, tmp_path: Path) -> None:
        # The anti-over-block twin: converting failures into retries must not make a
        # legitimate call fail. Without it the rows above are satisfied by a toolset
        # that refuses everything.
        (tmp_path / "a.py").write_text("x = 1\n")

        messages = self._run(self._reads_then_answers("a.py"), tmp_path)

        assert self._errors(messages) == []
        assert next(m for m in messages if isinstance(m, ResultMessage)).result == "done"

    def test_a_teatree_bug_still_propagates_untouched(self, tmp_path: Path) -> None:
        # The boundary: ONLY the model's own input errors are converted. A defect in
        # teatree's code must still land as the driver's sdk_error traceback rather
        # than be laundered into "the model got it wrong" and silently retried.
        def broken(path: str) -> str:
            msg = f"a genuine bug in teatree, reading {path}"
            raise ZeroDivisionError(msg)

        inner: FunctionToolset[None] = FunctionToolset()
        inner.add_function(broken, takes_ctx=False, name="Read")
        agent: Agent[None, str] = Agent(
            self._reads_then_answers("a.py"), toolsets=[HardDenyToolset(inner, cwd=tmp_path)]
        )
        session = PydanticAiHarnessSession(agent, model_name="test", phase="coding")

        async def drive() -> list[object]:
            await session.query("go")
            return [message async for message in session.receive_response()]

        with pytest.raises(ZeroDivisionError):
            asyncio.run(drive())


class TestDenialCountsBounded:
    """``denial_counts`` stays bounded and never grows a shared ``""`` bucket (#81)."""

    def test_tally_is_capped_across_many_runs(self) -> None:
        # One deny per run over more distinct runs than the cap: without a bound the
        # dict would grow one key per run forever; the reaper keeps it within the cap.
        toolset = HardDenyToolset(FunctionToolset(), max_denials=2)
        for i in range(_MAX_TRACKED_RUNS + 50):
            with pytest.raises(ModelRetry):
                asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, SimpleNamespace(run_id=f"run-{i}"), None))
        assert len(toolset.denial_counts) <= _MAX_TRACKED_RUNS

    def test_missing_run_id_does_not_create_an_empty_key_bucket(self) -> None:
        toolset = HardDenyToolset(FunctionToolset(), max_denials=2)
        with pytest.raises(ModelRetry):
            asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, SimpleNamespace(), None))
        assert "" not in toolset.denial_counts

    def test_two_run_id_less_runs_do_not_share_a_tally(self) -> None:
        # Distinct contexts with no run_id must not pool into one bucket and trip the
        # cap early — each keys off its own context identity. Both are held live so
        # their ids cannot be reused between the two calls.
        toolset = HardDenyToolset(FunctionToolset(), max_denials=2)
        ctx_a, ctx_b = SimpleNamespace(), SimpleNamespace()
        with pytest.raises(ModelRetry):
            asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, ctx_a, None))
        with pytest.raises(ModelRetry):
            asyncio.run(toolset.call_tool("Bash", _DENIED_CALL, ctx_b, None))


class TestSoftGate:
    def test_predicate_matches_only_gated_names(self) -> None:
        predicate = make_soft_gate_predicate(frozenset({"Bash"}))
        assert predicate(None, _def("Bash"), {}) is True
        assert predicate(None, _def("Read"), {}) is False

    def test_raise_if_soft_gated_raises_approval_required(self) -> None:
        with pytest.raises(ApprovalRequired):
            raise_if_soft_gated("Bash", frozenset({"Bash"}))

    def test_ungated_name_does_not_raise(self) -> None:
        raise_if_soft_gated("Read", frozenset({"Bash"}))  # must not raise


def _def(name: str) -> ToolDefinition:
    return ToolDefinition(name=name)
