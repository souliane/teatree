"""Prose that states what the coordinator program does, kept as written in the tracked files (#5685)."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ANSWERER_SKILL = "skills/answerer/SKILL.md"
HEADLESS_EXECUTOR_DOC = "docs/blueprint/headless-executor.md"


def _text(relative_path: str) -> str:
    return " ".join((ROOT / relative_path).read_text(encoding="utf-8").split())


def test_the_answerer_skill_says_message_peer_alone_answers_an_open_request_off_a_conversation() -> None:
    sentence = "Off a conversation, `message_peer` alone answers an open request."

    assert sentence in _text(ANSWERER_SKILL), f"{ANSWERER_SKILL} no longer says: {sentence}"


@pytest.mark.parametrize(
    "sentence",
    [
        pytest.param(
            "allow `agent_mailbox_send`, `agent_mailbox_inbox`, `agent_mailbox_wait` and `escalate` by name; these "
            "change peer delivery state without writing to the repository, and other read-only phases retain only "
            "mailbox self and peers.",
            id="read-only-phases-allow-escalate-by-name",
        ),
        pytest.param(
            "`escalate` takes a declared kind and one message: it writes one internal `DeferredQuestion` request "
            "first (a repeat of the same call returns the same row), then tells a live answering peer of the ticket "
            "over the mailbox, else resumes the thread's newest answering task with the fenced request as its first "
            "input.",
            id="escalate-writes-the-request-then-tells-the-answerer",
        ),
        pytest.param(
            "While the request is open the owner's next message in the bound thread goes to the coordinator instead "
            "of the asking task (pause and resume still reach the task).",
            id="the-owners-next-message-goes-to-the-coordinator",
        ),
        pytest.param(
            "Past a constant deadline (`ESCALATION_DEADLINE`, no setting) `settle_expired_escalations` raises one "
            "ordinary owner card when the kind is owner-raisable, otherwise fails the asking task with an "
            "`escalation_unserved:` reason and records no card.",
            id="the-deadline-raises-a-card-or-fails-the-asking-task",
        ),
        pytest.param(
            "The answer does not travel back through this tool: an answering coordinator holds no write tool, so it "
            "replies by proposing `message_peer`.",
            id="the-answer-comes-back-as-message_peer",
        ),
        pytest.param(
            "On the asker's own ticket, while the request is open and the asker is the one task running there "
            "outside the answering phase, the brief and the escalation text offer that kind alone, for that ticket "
            "only; the text goes to the asking task and, once it accepts, closes the request, so the deadline then "
            "raises no card and fails no task, except that a request of one of the four kept limits is refused and "
            "stays open, so the deadline raises the owner card.",
            id="the-reply-is-offered-alone-and-a-kept-limit-stays-open",
        ),
        pytest.param(
            "A request nobody answers ends at its deadline or when the asker finishes.",
            id="an-unanswered-request-ends-at-its-deadline",
        ),
        pytest.param(
            "The tool never reaches the answering phase, and it cannot spend money, delete unremoted work, widen a "
            "credential's use or message a colleague",
            id="the-tool-never-crosses-an-owner-limit",
        ),
    ],
)
def test_the_headless_executor_page_states_the_escalate_tool_as_this_program_built_it(sentence: str) -> None:
    assert sentence in _text(HEADLESS_EXECUTOR_DOC), f"{HEADLESS_EXECUTOR_DOC} no longer says: {sentence}"
