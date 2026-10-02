"""Shared leaf: orchestration-boundary command/origin signals (#115, #1442).

Two primitives that the orchestrator-boundary gate family reads to decide
whether a PreToolUse call should be governed:

* :func:`call_is_from_subagent` — main-agent vs sub-agent origin.
* :data:`PYTEST_VERB_RE` / :data:`PYTEST_VERB_FINDER` — a foreground
    test-run command shape.

They were inline in ``hook_router`` and are now extracted so BOTH the heavy-Bash
deny gate (``handle_enforce_orchestrator_boundary``) and the broader
orchestrator-investigation WARN nudge (``orchestrator_investigation_gate``) read
them from one shared leaf — neither gate family imports a private off the other,
and ``hook_router`` nets smaller. This is a dependency-free leaf: it imports
nothing first-party, so ``hook_router`` and the gate leaves import IT without a
cycle.
"""

import re
import sys

# Alias both identities so a bare ``from orchestration_boundary_signals import
# ...`` (the live hook, whose dir is on sys.path) and the
# ``hooks.scripts.orchestration_boundary_signals`` form (a subprocess/test
# import) resolve the SAME module object.
sys.modules.setdefault("orchestration_boundary_signals", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.orchestration_boundary_signals", sys.modules[__name__])


def call_is_from_subagent(data: dict) -> bool:
    """True when the gated tool call originates from a sub-agent.

    Main-vs-sub-agent signal (#115 root cause). The PreToolUse payload's
    ``transcript_path`` ALWAYS points at the PARENT session transcript, even for
    a sub-agent's tool call (a sub-agent's own turns live in a separate
    ``…/subagents/agent-<id>.jsonl`` the hook never receives), and the parent
    transcript's tail entries carry ``isSidechain: false`` — so a
    transcript-``isSidechain`` read MISDETECTS every genuine sub-agent as the
    main agent. The reliable signal is on the payload itself: a sub-agent call
    carries a non-empty ``agent_id`` (and ``agent_type``); a main-agent call
    omits it. Empty/absent ``agent_id`` ⇒ main agent.
    """
    return bool(data.get("agent_id"))


# ``pytest`` must match only in a VERB POSITION — never inside a quoted arg, a
# branch name, a ``-m``/``--title`` message, or a hyphenated package name
# (``pytest-django``). A bare ``\bpytest\b`` mis-matched a ``git commit -m 'fix
# pytest fixture'`` / ``git branch x-pytest`` / ``uv add pytest-django`` (#1178).
# So anchor it to a command head: start-of-string OR a shell separator (``;``
# ``&&`` ``||`` ``|`` newline ``(`` ``{``), then optional env-var assignments,
# optional (possibly-stacked) command-wrapper prefixes
# (``command``/``exec``/``time``/``nice``), and an optional Python runner prefix
# — note ``uvx`` runs a tool DIRECTLY with no ``run`` (``uvx pytest``), while
# ``uv``/``poetry``/``pdm``/``hatch`` DO need ``run``, and ``python[3] -m`` —
# then ``pytest`` NOT followed by a word char or hyphen. The separator branch
# keeps the shell-grammar bypass guard intact (``git status && pytest`` still
# matches); the trailing ``(?![\w-])`` keeps the match pinned to ``pytest`` so
# wrapper prefixes never widen to other tools (``uvx ruff`` stays unmatched).
PYTEST_VERB_RE = (
    r"(?:^|[;&|\n(){}])"
    r"\s*"
    r"(?:\w+=\S+\s+)*"
    r"(?:(?:command|exec|time|nice)\s+)*"
    r"(?:uvx\s+|(?:uv|poetry|pdm|hatch)\s+run\s+|python3?\s+-m\s+)?"
    r"pytest(?![\w-])"
)
PYTEST_VERB_FINDER = re.compile(PYTEST_VERB_RE)
