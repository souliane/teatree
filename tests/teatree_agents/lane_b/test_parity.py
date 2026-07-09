"""Lane parity eval — ``claude_sdk`` ↔ ``pydantic_ai`` yield the same vocabulary.

The PR-03 acceptance criteria, proven with zero tokens.

(a) A scripted ``FunctionModel`` trajectory (``ALLOW_MODEL_REQUESTS = False``)
drives the REAL :class:`PydanticAiHarness` session end-to-end, and the messages
it yields are the SAME ``claude_agent_sdk`` vocabulary the ``claude_sdk`` lane
yields — a tool call surfaces as a :class:`ToolUseBlock`, its result as a
:class:`ToolResultBlock`, the final text as a :class:`TextBlock`, closed by a
:class:`ResultMessage`.

(c) A hard-deny gate (main-clone mutation) fires on Lane B through the SAME
shared :func:`hard_deny_reason` the ``claude_sdk`` lane's PreToolUse hook
consults, surfacing an ``is_error`` :class:`ToolResultBlock`.
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pydantic_ai.models
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import hooks.scripts.no_self_reviewer_assign as _reviewer_guard
import hooks.scripts.secret_file_print_guard as _secret_guard
from hooks.scripts.direct_command_guard import deny_match as _direct_deny_match
from hooks.scripts.quote_verdict import resolve_high_verdict
from hooks.scripts.raw_review_post_guard import is_raw_review_write as raw_review_write_lane_a
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.lane_b.gating import hard_deny_reason
from teatree.hooks import (
    _repo_visibility,
    raw_merge_detect,
    raw_review_post_detect,
    safe_kill_detect,
    secret_file_print_detect,
    self_reviewer_assign_detect,
)
from teatree.hooks.quote_scanner import extract_publish_payload, scan_text
from tests.teatree_agents.lane_b._managed_clone import linked_worktree, managed_main_clone
from tests.test_hook_router_classifier_relax_wire import run_hook_router

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # ty: ignore[invalid-assignment] — the zero-token test guard.

# Lane-A Bash-shaped deny matchers, imported from the cold PreToolUse guards — the
# INDEPENDENT implementations the Lane-B leaves must agree with. raw-merge and
# raw-pid-kill are already unified (the router guards DELEGATE to the leaves), so
# their "Lane A denies" proof reads the shared detector; the other four guards keep
# their own matcher, making the parity check a genuine cross-implementation compare.
bash_assigns_reviewer_lane_a = _reviewer_guard._bash_assigns_reviewer
_secret_print_lane_a = _secret_guard._is_secret_print


def _lane_a_direct_denies(command: str) -> bool:
    return _direct_deny_match(command) is not None


def _lane_a_secret_denies(command: str) -> bool:
    return _secret_print_lane_a(command)


def _lane_a_review_denies(command: str) -> bool:
    return raw_review_write_lane_a(command)


def _lane_a_reviewer_denies(command: str) -> bool:
    return bash_assigns_reviewer_lane_a(command)


def _lane_a_raw_merge_denies(command: str) -> bool:
    # The router's out-of-band-merge gate delegates its detection to
    # raw_merge_detect and denies when the repo is managed; the detector firing IS
    # the Lane-A deny trigger (Lane B is always jailed to a managed worktree).
    return raw_merge_detect.raw_merge_deny_reason(command) is not None


def _lane_a_pid_kill_denies(command: str) -> bool:
    return safe_kill_detect.detect_raw_pid_kill(command).is_raw_pid_kill


def _streaming_model(*, tool_command: str) -> FunctionModel:
    """A streaming FunctionModel: call ``shell`` with *tool_command*, then text."""
    state = {"n": 0}

    def stream_fn(messages: object, info: object) -> object:
        state["n"] += 1
        turn = state["n"]

        async def gen():  # noqa: RUF029 — an async generator (the stream contract) that only yields.
            if turn == 1:
                args = json.dumps({"command": tool_command})
                yield {0: DeltaToolCall(name="shell", json_args=args, tool_call_id="c1")}
            else:
                yield "done"

        return gen()

    return FunctionModel(stream_function=stream_fn)


def _collect(harness: PydanticAiHarness, options: ClaudeAgentOptions, prompt: str) -> list[object]:
    async def run() -> list[object]:
        async with harness.open(options) as session:
            await session.query(prompt)
            return [message async for message in session.receive_response()]

    return asyncio.run(run())


def _blocks(messages: list[object], block_type: type) -> list[object]:
    return [
        block
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, block_type)
    ]


class TestVocabularyParity:
    def test_safe_tool_call_yields_the_sdk_message_vocabulary(self, tmp_path: Path) -> None:
        (tmp_path / "marker").write_text("")
        harness = PydanticAiHarness(model=_streaming_model(tool_command="ls"), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(tmp_path)), "list the dir")

        # Every yielded message is a claude_agent_sdk type — the seam's contract.
        assert all(isinstance(m, (AssistantMessage, ResultMessage)) for m in messages)
        # The tool call + its result surfaced in the seam's tool-block vocabulary.
        tool_uses = _blocks(messages, ToolUseBlock)
        assert [t.name for t in tool_uses] == ["shell"]
        tool_results = _blocks(messages, ToolResultBlock)
        assert tool_results
        assert any("marker" in str(r.content) for r in tool_results)
        # The final text + a terminal ResultMessage.
        assert [b.text for b in _blocks(messages, TextBlock)] == ["done"]
        assert isinstance(messages[-1], ResultMessage)


class TestHardDenyParity:
    def test_main_clone_mutation_is_refused_when_cwd_is_a_managed_main_clone(self, tmp_path: Path) -> None:
        command = "git reset --hard HEAD~1"
        clone = managed_main_clone(tmp_path / "teatree")
        # The shared evaluator the claude_sdk lane's PreToolUse hook also consults.
        assert hard_deny_reason("shell", {"command": command}, cwd=clone) is not None

        harness = PydanticAiHarness(model=_streaming_model(tool_command=command), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(clone)), "reset hard")

        error_results = [r for r in _blocks(messages, ToolResultBlock) if r.is_error]
        assert error_results, "a refused tool call must surface an is_error ToolResultBlock"
        assert any("BLOCKED" in str(r.content) for r in error_results)

    def test_same_mutation_runs_in_a_linked_worktree(self, tmp_path: Path) -> None:
        # The Lane-B jail root is the WORKTREE, so the same op Lane A allows there
        # is NOT refused: no is_error ToolResultBlock, and the command executes.
        command = "git reset --hard HEAD"
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        assert hard_deny_reason("shell", {"command": command}, cwd=wt) is None

        harness = PydanticAiHarness(model=_streaming_model(tool_command=command), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(wt)), "reset soft")

        assert not [r for r in _blocks(messages, ToolResultBlock) if r.is_error]


def _pairing_validator_model(orphans: list[str]) -> FunctionModel:
    """A streaming FunctionModel that records every orphaned tool-result it is sent.

    A ``ToolReturnPart`` / tool-linked ``RetryPromptPart`` whose ``tool_call_id``
    was not produced by a preceding ``ToolCallPart`` is exactly the "tool message
    without preceding tool_calls" an OpenAI-compatible provider rejects — the model
    stand-in for that wire-level check.
    """

    def stream_fn(messages: object, info: object) -> object:
        call_ids: set[str] = set()
        for message in messages:  # type: ignore[attr-defined]
            if isinstance(message, ModelResponse):
                call_ids.update(p.tool_call_id for p in message.parts if isinstance(p, ToolCallPart))
            elif isinstance(message, ModelRequest):
                for part in message.parts:
                    tool_linked_retry = isinstance(part, RetryPromptPart) and part.tool_name is not None
                    if (isinstance(part, ToolReturnPart) or tool_linked_retry) and part.tool_call_id not in call_ids:
                        orphans.append(part.tool_call_id)

        async def gen():  # noqa: RUF029 — an async generator (the stream contract) that only yields.
            yield "ok"

        return gen()

    return FunctionModel(stream_function=stream_fn)


def _history_straddling_a_tool_pair() -> list[ModelMessage]:
    """A 44-message history whose default (``keep_recent=40``) cut orphans a return.

    The kept window is the last 40 (indices 4..43): the tool RETURN sits at index 4
    (first kept) while its CALL at index 3 falls in the dropped middle, so a naive
    ``[first, *last-40]`` keeps an orphaned return. Index 5 onward is filler so the
    snapped window opens on a non-tool message.
    """
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="task")]),  # 0: framing
        ModelResponse(parts=[TextPart(content="a1")]),  # 1: dropped middle
        ModelRequest(parts=[UserPromptPart(content="u2")]),  # 2: dropped middle
        ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"command": "ls"}, tool_call_id="c1")]),  # 3: CALL
        ModelRequest(parts=[ToolReturnPart(tool_name="shell", content="out", tool_call_id="c1")]),  # 4: RETURN
    ]
    for i in range(5, 44):
        if i % 2 == 1:
            history.append(ModelResponse(parts=[TextPart(content=f"a{i}")]))
        else:
            history.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
    return history


class TestCompactionRoundTrip:
    def test_compacted_history_round_trips_with_no_orphaned_tool_return(self, tmp_path: Path) -> None:
        # The REAL PydanticAiHarness compacts the seeded history before the turn
        # (phase set → Lane-B tool layer + compaction). A validator FunctionModel
        # stands in for the OpenAI-compatible provider's tool-pairing check.
        orphans: list[str] = []
        harness = PydanticAiHarness(
            model=_pairing_validator_model(orphans),
            history=_history_straddling_a_tool_pair(),
            phase="coding",
        )
        _collect(harness, ClaudeAgentOptions(cwd=str(tmp_path)), "continue")

        assert orphans == [], f"the compacted history sent to the model orphaned a tool-return: {orphans}"


class TestPrivacyGateParity:
    """The privacy/banned-term gate refuses the SAME publish set on both lanes.

    Lane A's PreToolUse scopes the scan to :func:`extract_publish_payload` (``None``
    for a non-publish call) and routes a HIGH finding through its OWN destination
    verdict (:func:`resolve_high_verdict`: SKIP a non-public / unresolvable target,
    DOWNGRADE a provably-private one, DENY only a confirmed-public one). Lane B's
    :func:`hard_deny_reason` now consults the same scoping AND the same destination
    gate, so the two lanes refuse the identical set — a local write / non-publish
    shell command on NEITHER, a clean publish on NEITHER, a HIGH-content publish to
    a non-public / unresolvable target on NEITHER, and a HIGH-content publish to a
    confirmed-PUBLIC target on BOTH (the hard anti-leak constraint).
    """

    _HIGH_BODY = "the user said: do it now"  # trips the ``the-user-said-colon`` HIGH pattern

    @pytest.fixture(autouse=True)
    def _hermetic_visibility(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Isolate the config home + the visibility cache so a monkeypatched probe
        # verdict governs, never the developer's real config or a warm cache.
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "viscache"))

    def _lane_a_denies(self, tool_name: str, tool_args: dict, cwd: Path | None) -> bool:
        # Lane A's REAL deny predicate: the publish payload (else None), a HIGH scan,
        # then Lane A's OWN destination verdict — the exact ``resolve_high_verdict``
        # function its PreToolUse hook consults, NOT a strawman that omits the
        # destination gate and so would call every HIGH finding a deny.
        command = tool_args.get("command", "") if tool_name == "shell" else ""
        payload = extract_publish_payload("Bash", {"command": command}, cwd) if command else None
        if payload is None or not scan_text(payload).has_high:
            return False
        return resolve_high_verdict(command, cwd).deny

    @staticmethod
    def _post(slug: str, body: str) -> dict:
        return {"command": f'gh pr comment 5 --repo {slug} --body "{body}"'}

    def test_local_write_with_a_high_finding_is_refused_on_neither_lane(self, tmp_path: Path) -> None:
        # RED without the payload-scoping fix: Lane B scanned every string arg, so
        # write_file's content tripped HIGH and was denied while Lane A never scans
        # a local write.
        args = {"path": "note.md", "content": self._HIGH_BODY}
        assert hard_deny_reason("write_file", args, cwd=tmp_path) is None
        assert self._lane_a_denies("write_file", args, tmp_path) is False

    def test_non_publish_shell_command_with_a_high_finding_is_refused_on_neither_lane(self, tmp_path: Path) -> None:
        # A local `echo ... > file` is not a publish — Lane A passes it through, and
        # Lane B must too (RED without the fix: the whole command string was scanned).
        args = {"command": f'echo "{self._HIGH_BODY}" > note.md'}
        assert hard_deny_reason("shell", args, cwd=tmp_path) is None
        assert self._lane_a_denies("shell", args, tmp_path) is False

    def test_clean_publish_command_is_refused_on_neither_lane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        args = self._post("souliane/teatree", "shipped the compaction fix")
        assert hard_deny_reason("shell", args, cwd=tmp_path) is None
        assert self._lane_a_denies("shell", args, tmp_path) is False

    def test_public_target_high_finding_is_denied_on_both_lanes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The hard anti-leak constraint: a HIGH body to a CONFIRMED-PUBLIC egress is
        # STILL denied on Lane B (matching Lane A) — public-egress protection intact.
        # This is the anti-vacuity guard for the two allow rows below.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        args = self._post("souliane/teatree", self._HIGH_BODY)
        reason = hard_deny_reason("shell", args, cwd=tmp_path)
        assert reason is not None
        assert "privacy/banned-term gate" in reason
        assert self._lane_a_denies("shell", args, tmp_path) is True

    def test_private_target_high_finding_is_allowed_on_both_lanes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A HIGH body to a probe-CONFIRMED-PRIVATE repo cannot leak to the public —
        # Lane A downgrades it, and Lane B now allows it too (no over-deny).
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PRIVATE")
        args = self._post("someowner/private-svc", self._HIGH_BODY)
        assert hard_deny_reason("shell", args, cwd=tmp_path) is None
        assert self._lane_a_denies("shell", args, tmp_path) is False

    def test_unresolvable_target_high_finding_is_allowed_on_both_lanes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The allow-on-unknown anti-vacuity proof — RED before the fix, where Lane B
        # denied ANY HIGH finding while Lane A skips an unresolvable target (bias to
        # not firing). A cold-hook target whose visibility cannot be resolved is NOT
        # affirmatively public, so both lanes ALLOW it.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: None)
        args = self._post("someowner/mystery", self._HIGH_BODY)
        assert hard_deny_reason("shell", args, cwd=tmp_path) is None
        assert self._lane_a_denies("shell", args, tmp_path) is False


class TestBashHardDenyCorpusParity:
    """Every Lane-A Bash-shaped hard-deny fires identically on Lane B (souliane/teatree#2).

    The structural capstone for the "Lane-B bypass" class: before the shared
    :mod:`teatree.hooks.hard_deny_registry`, Lane B's :func:`hard_deny_reason`
    checked only main-clone + privacy, so a raw ``gh pr merge`` /
    ``git push --no-verify`` / secret-print / raw-review-post / reviewer-assign /
    raw-pid-kill was reachable under ``agent_harness=pydantic_ai`` with NO
    MergeClear or CI verification. This test feeds each Lane-A deny fixture through
    Lane B's ``hard_deny_reason`` (via the SAME registry) and asserts an identical
    refusal — and, per family, feeds a fixture through the Lane-A guard matcher AND
    the Lane-B leaf and asserts they agree, so a future divergence fails CI.
    """

    # (label, command, lane_a_denies) — every row is a REAL Lane-A Bash-shaped deny
    # (the last field is the ANTI-VACUITY proof: the Lane-A guard denies it too).
    _CORPUS: tuple[tuple[str, str, Callable[[str], bool]], ...] = (
        ("raw gh pr merge", "gh pr merge 5 --repo o/x", _lane_a_raw_merge_denies),
        ("raw glab mr merge", "glab mr merge 7", _lane_a_raw_merge_denies),
        ("raw merge api PUT", "gh api repos/o/x/pulls/7/merge -X PUT", _lane_a_raw_merge_denies),
        ("git commit no-verify", "git commit -m x --no-" + "verify", _lane_a_direct_denies),
        ("git push no-verify", "git push --no-" + "verify", _lane_a_direct_denies),
        ("git hooksPath silencer", "git -c core.hooks" + "Path=/dev/null commit -m x", _lane_a_direct_denies),
        ("secret cat netrc", "cat ~/.netrc", _lane_a_secret_denies),
        ("pass show unredirected", "pass show ci/token", _lane_a_secret_denies),
        (
            "raw review POST",
            "glab api projects/42/merge_requests/7/discussions -X POST -f body=hi",
            _lane_a_review_denies,
        ),
        ("glab reviewer assign", "glab mr update 7 --reviewer alice", _lane_a_reviewer_denies),
        ("gh reviewer assign", "gh pr edit 7 --add-reviewer bob", _lane_a_reviewer_denies),
        ("raw pid kill", "kill -9 4242", _lane_a_pid_kill_denies),
    )

    @pytest.mark.parametrize(("label", "command", "lane_a_denies"), _CORPUS, ids=[row[0] for row in _CORPUS])
    def test_lane_b_denies_every_lane_a_bash_hard_deny(
        self, label: str, command: str, lane_a_denies: Callable[[str], bool], tmp_path: Path
    ) -> None:
        # Anti-vacuity: the fixture is a genuine Lane-A refusal.
        assert lane_a_denies(command), f"{label}: fixture is not a Lane-A deny — the parity claim is vacuous"
        # RED before the registry wiring: Lane B returned None (the command was reachable).
        reason = hard_deny_reason("shell", {"command": command}, cwd=tmp_path)
        assert reason is not None, f"{label}: Lane B must refuse a command Lane A refuses (the bypass class)"
        assert "BLOCKED" in reason

    def test_benign_commands_are_allowed_on_lane_b(self, tmp_path: Path) -> None:
        # The anti-over-block twin: a benign shell command Lane A allows is allowed.
        for command in ("ls -la", "gh pr view 5", "git commit -m 'normal'", "gh api repos/o/x/pulls/7/comments"):
            assert hard_deny_reason("shell", {"command": command}, cwd=tmp_path) is None, command

    # (leaf-predicate, INDEPENDENT lane-a-matcher, commands) — the divergence guard:
    # for each command (deny AND allow shapes), the Lane-B leaf and the Lane-A guard
    # (a separate hooks/scripts implementation) must agree. raw-merge and raw-pid-kill
    # are omitted here — the router guards DELEGATE to those leaves, so there is no
    # independent implementation to diverge (the corpus test covers them).
    def test_each_leaf_agrees_with_its_independent_lane_a_guard(self) -> None:
        pairs: tuple[tuple[Callable[[str], bool], Callable[[str], bool], tuple[str, ...]], ...] = (
            (
                secret_file_print_detect.is_secret_print,
                _secret_print_lane_a,
                ("cat ~/.netrc", "pass show ci/token", "cat README.md", "TOKEN=$(pass show x)", "ls"),
            ),
            (
                raw_review_post_detect.is_raw_review_write,
                raw_review_write_lane_a,
                (
                    "glab api projects/42/merge_requests/7/discussions -X POST -f body=hi",
                    "glab api projects/42/merge_requests/7/discussions",
                    "gh api repos/o/x/pulls/7/comments -X GET",
                    "ls",
                ),
            ),
            (
                self_reviewer_assign_detect.bash_assigns_reviewer,
                bash_assigns_reviewer_lane_a,
                (
                    "glab mr update 7 --reviewer alice",
                    "gh pr create --reviewer bob",
                    "glab api projects/1/merge_requests/2 -X PUT -f reviewer_ids=3",
                    "gh api repos/o/x/pulls/7/requested_reviewers",
                    "git commit -m 'note about --reviewer'",
                ),
            ),
        )
        for leaf_matcher, guard_matcher, commands in pairs:
            for command in commands:
                assert leaf_matcher(command) == guard_matcher(command), command


class TestBashHardDenyWireParity:
    """Wire-level: a command Lane A's real PreToolUse subprocess denies, Lane B denies too.

    Reuses GM-1's ``run_hook_router`` subprocess harness so the check runs through
    the actual ``hook_router.main()`` exit-code translation (not the in-process
    matcher), closing the "tests exercise the unit but never the wire" pattern for
    this seam. ``git … --no-verify`` is the fixture: the direct-command guard denies
    it unconditionally (no cwd carve-out), so the subprocess reliably exits 2.
    """

    def test_no_verify_denied_at_the_wire_and_on_lane_b(self, tmp_path: Path) -> None:
        command = "git commit -m x --no-" + "verify"
        home = str(tmp_path / "home")
        Path(home).mkdir(parents=True, exist_ok=True)
        result = run_hook_router("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": command}}, home=home)
        assert result.returncode == 2, f"Lane A's PreToolUse subprocess must deny --no-verify; got {result.returncode}"
        # The SAME command is refused on Lane B through the shared registry.
        assert hard_deny_reason("shell", {"command": command}, cwd=tmp_path) is not None


def test_zero_tokens_enforced() -> None:
    assert pydantic_ai.models.ALLOW_MODEL_REQUESTS is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
