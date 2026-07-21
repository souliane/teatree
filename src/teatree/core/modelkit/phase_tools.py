"""Per-phase tool least-privilege table — one source of truth for both lanes.

The canonical phase vocabulary (:mod:`teatree.core.modelkit.phases`) says WHICH
sub-agent runs a ``(role, phase)`` pair; this module says WHICH tools that phase
may call. It is the single source of truth consumed by BOTH runtime lanes: Lane
B (``pydantic_ai``, PR-03) filters its assembled toolsets down to
:func:`tools_for_phase` — a phase only sees the tools it is allowed; Lane A
(``claude_sdk``, PR-11) injects the COMPLEMENT
(:func:`disallowed_tools_for_phase`) as ``ClaudeAgentOptions.disallowed_tools``
so the same least-privilege holds on the SDK transport.

The names here are teatree's OWN capability tool names (the Lane-B
``FunctionToolset`` tool names in :mod:`teatree.agents.lane_b`), which are the
provider-neutral vocabulary; Lane A maps each to its SDK-native equivalent at
its own boundary. ``normalize_phase`` collapses spellings so a table keyed on the
canonical token resolves a task stored with any accepted alias.
"""

from typing import Final

from teatree.core.modelkit.phases import normalize_phase

#: Every capability tool name Lane B can expose. A phase's allowance is a subset;
#: the complement (universe minus allowance) is the disallow list Lane A injects.
ALL_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "shell",
        "web_fetch",
        "web_search",
        "dispatch_subtask",
        "recall_memory",
        "record_attempt",
    }
)

# Reusable capability bundles, composed into per-phase allowances below.
#: The EMPTY toolset — the quarantined ``directive_reading`` reader profile (#116).
#: A phase mapped to ``_NONE`` may call NOTHING: Lane B filters its toolset to
#: empty, and Lane A injects the FULL complement (``ALL_TOOLS``) as
#: ``disallowed_tools`` — every SDK built-in (Read/Write/Edit/Grep/Glob/Bash/
#: WebFetch/WebSearch/Agent/Task) is denied. The reader that ingests untrusted
#: content physically cannot read a file, shell out, fetch a URL, write, or spawn a
#: sub-agent — it cannot act or exfiltrate regardless of what the content tells it.
_NONE: Final[frozenset[str]] = frozenset()
_READ_ONLY: Final[frozenset[str]] = frozenset({"read_file", "search_files", "recall_memory"})
_WEB: Final[frozenset[str]] = frozenset({"web_fetch", "web_search"})
_WRITE: Final[frozenset[str]] = frozenset({"write_file", "edit_file"})
_FULL: Final[frozenset[str]] = ALL_TOOLS
#: The read-mostly-WITH-shell shape every verdict-producing review phase carries:
#: read + search + web + shell, and NEVER write/edit (a review does not mutate
#: source). Shared by :data:`VERDICT_REVIEW_PHASES` below.
_REVIEW_WITH_SHELL: Final[frozenset[str]] = _READ_ONLY | _WEB | {"shell"}

#: Every review phase whose DELIVERABLE is a recorded verdict. All four share one
#: read-mostly-with-shell grant (:data:`_REVIEW_WITH_SHELL`), applied from this set
#: so the entries cannot drift apart. The shell is load-bearing for the ``codex_*``
#: variants specifically: they have NO server-side envelope seam (they are not in
#: ``attempt_recorder._REVIEW_VERDICT_PHASES``) and the MCP post path
#: (:class:`teatree.cli.review.service.ReviewService`) is GitLab-only, so on a
#: GitHub PR the shell (``t3 teatree review record`` / ``t3 teatree review
#: post-comment``, bound to a ``git rev-parse HEAD`` sha off a ``git worktree add
#: --detach`` cold checkout) is their ONLY way to deliver a verdict — a shell-less
#: codex member reads the diff but never delivers, stalling and leaking an "I have
#: no Bash/git/gh" question to the owner. ``reviewing`` and ``e2e_reviewing`` carry
#: the same shell grant and additionally have that envelope seam. ``requesting_review``
#: is deliberately NOT a member: it records no verdict and stays plain read-only.
VERDICT_REVIEW_PHASES: Final[frozenset[str]] = frozenset(
    {"reviewing", "codex_reviewing", "codex_adversarial_reviewing", "e2e_reviewing"}
)

#: Canonical phase -> the exact set of capability tool names it may call.
#: A read-mostly phase (requesting_review, scanning_news, answering) has NO
#: write/edit/shell — the least-privilege both lanes enforce.
#: A write phase (coding, testing, e2e, debugging) gets the full set. ``bughunt``
#: executes to reproduce a candidate but never writes (shell + dispatch, no
#: write/edit). ``planning`` gets the shell (no write/edit) so the planner can do
#: honest git archaeology — fetch, log, base_sha capture. Every
#: :data:`VERDICT_REVIEW_PHASES` member gets the read-mostly-with-shell shape from
#: one shared entry: the reviewer skills require the shell to fetch the exact pushed
#: head (the ``git worktree add --detach`` cold-review checkout), run ``t3 tool
#: verify-gates`` / ``git`` / ``git log -S`` archaeology, and RECORD the verdict via
#: ``t3 teatree review record`` / ``t3 teatree review post-comment`` — with NO write/edit (a review
#: never mutates source), so they stay least-privilege while being ABLE to produce a
#: merge_safe/hold verdict (F4). The teatree MCP review
#: tools (``mcp__teatree__github_pr_diff`` / ``review_post_comment`` /
#: ``task_complete``) are MCP-server tools, not built-in capabilities, so they are
#: never in the disallow complement and reach the spawn independently of this table.
#: An unknown phase falls back to read-only (:func:`tools_for_phase`) —
#: deny-by-default, so a new phase never silently inherits shell/write until it is
#: added here. TOTALITY: every dispatchable ``SUBAGENT_BY_PHASE`` phase MUST have an
#: explicit entry here (the ``test_registry_parity`` totality lane), so the
#: read-only fallback is defense-in-depth for a genuinely unregistered phase, never
#: the silent resolution for a dispatchable one (#10).
_TOOLS_BY_PHASE: Final[dict[str, frozenset[str]]] = {
    "planning": _READ_ONLY | _WEB | {"dispatch_subtask", "shell"},
    "scoping": _READ_ONLY | _WEB,
    "coding": _FULL,
    "testing": _FULL,
    "e2e": _FULL,
    "debugging": _FULL,
    # One grant for all four verdict-producing review phases, applied from the
    # named set so they can never drift apart again — the drift this closes gave
    # the HARDER ``codex:adversarial-review`` variant (selected for the
    # highest-stakes diffs: auth/, permissions/, migrations/, secrets) a WEAKER
    # toolset than the plain ``codex:review`` it shares a dispatch handler with,
    # so it came up shell-less, could not check out the head or record a verdict,
    # and leaked "I have no Bash/git/gh" questions to the owner instead.
    **dict.fromkeys(VERDICT_REVIEW_PHASES, _REVIEW_WITH_SHELL),
    "requesting_review": _READ_ONLY,
    "scanning_news": _READ_ONLY | _WEB,
    # The triage assessor reads local files/clone (Read/Grep) and WebFetches the
    # public issue page; it is shell-denied and never acts — so read-only + web,
    # NO shell/write. It hands recommendations back through the typed envelope.
    "triage_assessing": _READ_ONLY | _WEB,
    "critic_reviewing": _READ_ONLY | _WEB,
    # North-star PR-6 directive interpreter: read-only + codebase search only — it
    # finds the real core seam and drafts a sketch, never edits or shells out.
    "directive_interpreting": _READ_ONLY | _WEB,
    # #116 context firewall: the quarantined reader that ingests UNTRUSTED content
    # gets the EMPTY toolset (no tools of any kind). This MUST be an explicit entry —
    # the deny-by-default fallback is the NON-empty read-only bundle, so an
    # unregistered ``directive_reading`` would silently grant the reader file reads.
    # The totality lane (``test_registry_parity``) requires it be explicit.
    "directive_reading": _NONE,
    "bughunt": _READ_ONLY | {"shell", "dispatch_subtask"},
    "shipping": _READ_ONLY | {"shell", "record_attempt"},
    "answering": _READ_ONLY | _WEB,
    "retro": _READ_ONLY | _WRITE,
    # Scanner-dispatched phases (#3386): a loop scanner writes these directly to
    # ``Task.phase`` (``execution_target=HEADLESS``), OUTSIDE ``SUBAGENT_BY_PHASE`` —
    # ``phases.SCANNER_DISPATCHED_PHASES`` makes the totality lane see them as
    # producers, so an EXPLICIT entry here is REQUIRED, never the deny-by-default
    # read-only fallback resolving a dispatchable phase silently.
    #
    # ``architectural_review`` (the periodic ``ac-reviewing-codebase`` pass) is a
    # genuine review-WORK phase, so it gets the SAME read-mostly-with-shell shape as
    # the reviewer phases: its skill walks the whole tree (Read/Grep), does git/PR
    # archaeology (``git log``, merge-count since the last review), runs
    # ``t3 tool verify-gates``, and files findings through the normal ticket pipeline
    # — none of which the earlier NO-shell grant could do, which is exactly why a
    # dispatched review stalled and leaked an "I lack shell + have no checkout"
    # question to the owner. It keeps NO write/edit — a review produces tickets, not
    # commits (a BLUEPRINT staleness fix goes through the normal pipeline) — so it
    # stays least-privilege while being ABLE to complete the review. The dispatch
    # resolves its ``cwd`` to the overlay's main clone (``_resolve_task_cwd``), the
    # checkout the shell then reads and cold-worktrees from.
    "architectural_review": _READ_ONLY | _WEB | {"shell"},
    # ``dogfood_smoke`` shells out to ``t3 dogfood overlay-provision-smoke`` to run
    # the provision smoke, so it needs the shell (read-only+shell, mirroring
    # ``bughunt``/``shipping``); it never mutates source through the write tools.
    "dogfood_smoke": _READ_ONLY | {"shell"},
    # ``eval_local`` shells out to run the scoped eval suite (``t3 eval run``), the
    # same read-only+shell shape as ``dogfood_smoke``: it executes a suite and
    # reports, it never mutates source.
    "eval_local": _READ_ONLY | {"shell"},
    # ``backlog_sweep`` triages the issue tracker — it reads the tracker over the
    # forge CLI (shell) and the web, and records close/fold PROPOSALS behind the
    # scanner's ask-gate. NO write/edit: a sweep produces proposals, never commits.
    "backlog_sweep": _READ_ONLY | _WEB | {"shell"},
    # ``short_describe`` turns a cached issue title into a <=40 char statusline
    # summary. It is a pure text transformation over data already on the ``Ticket``
    # row, so it needs NO tool at all — and MUST NOT get the shell: the scanner
    # enqueues one per undescribed ticket, so granting it write/shell would turn a
    # few hundred summarisation dispatches a day into autonomous ticket-implementing
    # agents. Executed deterministically (``deterministic_phase_runner``) rather than
    # as a generic agent spawn, so the empty allowance is never contradicted by a
    # ticket-work brief.
    "short_describe": _NONE,
}


def tools_for_phase(phase: str) -> frozenset[str]:
    """Return the capability tool names *phase* may call.

    ``phase`` is normalized so a short-verb spelling resolves the same as the
    canonical gerund. An unknown phase falls back to the read-only bundle —
    deny-by-default, never the full set — so a phase added to the FSM without a
    table entry cannot silently acquire shell/write access.
    """
    return _TOOLS_BY_PHASE.get(normalize_phase(phase), _READ_ONLY)


def disallowed_tools_for_phase(phase: str) -> frozenset[str]:
    """Return the complement — the tools *phase* may NOT call (Lane A injects this)."""
    return ALL_TOOLS - tools_for_phase(phase)
