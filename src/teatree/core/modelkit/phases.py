"""Canonical phase vocabulary — one name set across skills, sessions, FSM (#694).

Skills emit short verbs (``scope``, ``code``, ``test``, ``review``, ``ship``,
``retro``); older code and ``Session._REQUIRED_PHASES`` use gerunds
(``testing``, ``reviewing``) and the short ``retro``. Before #694 these two
vocabularies were never reconciled, so ``visit-phase <id> review`` recorded an
unrecognised phase and never advanced the FSM.

``normalize_phase`` collapses every spelling to one canonical token (the form
already stored in ``Session.visited_phases`` / ``_REQUIRED_PHASES`` so existing
rows keep working). ``phase_transition`` maps a phase to the ``Ticket`` FSM
transition method that records it.

This module also owns the per-phase **fan-out** registry (teatree#2229):
``FANOUT_BY_PHASE`` maps a ``(role, phase)`` pair to a :class:`PhaseFanout`
spec, and ``resolve_fanout_directive`` renders the opt-in directive that the
loop threads into a sub-agent's prompt. The registry parallels
``SUBAGENT_BY_PHASE`` (every fan-out key MUST be a dispatched key) and is
default-OFF: with no ``[agent.phase_fanout]`` opt-in the resolver renders the
empty string, so dispatch is byte-identical to today.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only: ``resolve_fanout_directive`` receives a resolved
    # ``AgentConfig`` from the caller (loop-side), so ``core`` keeps NO runtime
    # import of the ``config_agent`` platform module. (The layered tach config
    # would actually permit a domain→platform edge; the decoupling is upheld by
    # convention + the import-isolation guard
    # ``test_core_phases_has_no_runtime_config_agent_import``, not by tach.) The
    # TYPE_CHECKING import is invisible to tach, mirroring the sibling
    # ``core.management.commands.loop_self_improve`` type-only import of
    # ``teatree.loop``.
    from teatree.config_agent import AgentConfig

# Canonical token -> every accepted alias (including the canonical itself).
_PHASE_ALIASES: dict[str, tuple[str, ...]] = {
    "planning": ("plan", "planning"),
    "scoping": ("scope", "scoping"),
    "coding": ("code", "coding"),
    "testing": ("test", "testing"),
    "reviewing": ("review", "reviewing"),
    "shipping": ("ship", "shipping"),
    "retro": ("retro", "retrospect", "retrospecting"),
    "requesting_review": ("request_review", "requesting_review", "request-review"),
    "e2e": ("e2e",),
    "e2e_reviewing": ("e2e-review", "e2e_review", "e2e_reviewing"),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canonical for canonical, aliases in _PHASE_ALIASES.items() for alias in aliases
}

#: The canonical phase vocabulary — the single source of truth a second
#: hand-maintained set (``Session._REQUIRED_PHASES``) must be validated
#: against so the two cannot drift silently (#782).
CANONICAL_PHASES: frozenset[str] = frozenset(_PHASE_ALIASES)

# Canonical phase -> the ``Ticket`` FSM transition method that records it.
_CANONICAL_TO_TRANSITION: dict[str, str] = {
    "planning": "plan",
    "scoping": "scope",
    "coding": "code",
    "testing": "test",
    "reviewing": "review",
    "shipping": "ship",
    "retro": "retrospect",
    "requesting_review": "request_review",
}


#: The single source of truth mapping a ticket ``(role, phase)`` pair to the
#: sub-agent the loop dispatches for it. Every author phase routes to its OWN
#: phase agent — the loop is the per-phase dispatcher, never a single
#: orchestrator that chains coder→tester→reviewer→shipper inline (BLUEPRINT
#: §5.2 / §17.8 invariant 10: phase agents are invoked by the headless
#: executor when a phase task is claimed; the orchestrator does synthesis and
#: dispatch, not execution). Keyed on the canonical phase token so a task
#: stored with any accepted spelling resolves through ``normalize_phase``.
SUBAGENT_BY_PHASE: dict[tuple[str, str], str] = {
    ("author", "planning"): "t3:planner",
    ("reviewer", "reviewing"): "t3:reviewer",
    ("author", "coding"): "t3:coder",
    ("author", "testing"): "t3:tester",
    ("author", "reviewing"): "t3:reviewer",
    ("author", "shipping"): "t3:shipper",
    ("author", "requesting_review"): "t3:review-request",
    ("author", "answering"): "t3:answerer",
    ("author", "scanning_news"): "t3:scanning-news",
    ("author", "e2e"): "t3:e2e",
    ("reviewer", "e2e_reviewing"): "t3:e2e-review",
    # Orthogonal reactive phase (like ``answering``/``scanning_news``): no FSM
    # transition, so not in ``_PHASE_ALIASES``/``CANONICAL_PHASES``. Registered
    # here so it is dispatchable and carries the find-then-verify fan-out below.
    ("author", "bughunt"): "t3:bughunter",
}

#: The chaining orchestrator must never be the target of an author phase
#: dispatch — that is the shadowing the per-phase restoration removes. A
#: conformance test asserts no author phase routes here.
CHAINING_ORCHESTRATOR: str = "t3:orchestrator"


def subagent_for_phase(role: str, phase: str) -> str:
    """Return the sub-agent for a ticket ``(role, phase)`` pair, or ``""``.

    ``phase`` is normalized so a task stored with a short-verb spelling
    (``code``/``test``/``review``/``ship``) resolves the same as the
    canonical gerund. An empty string means the pair has no registered
    sub-agent (operator triage) — the single authority both the loop
    dispatcher and the ``loop_dispatch`` command consult.
    """
    return SUBAGENT_BY_PHASE.get((role, normalize_phase(phase)), "")


#: The inclusive ``(min, max)`` bound on a fan-out's ``N`` (concurrent verifiers
#: / judges). ``2`` is the floor (a single agent is not a fan-out); ``5`` is the
#: ceiling so an int override cannot request an unbounded panel. An override
#: outside this range raises ``ValueError`` (fail-loud, like ``parse_effort``).
_FANOUT_N_BOUNDS: tuple[int, int] = (2, 5)


@dataclass(frozen=True)
class PhaseFanout:
    """The teatree-owned fan-out spec for one ``(role, phase)`` pair (#2229).

    *   ``pattern`` — the named dynamic-workflow shape (``adversarial-verify``,
        ``judge-panel``) the opted-in phase performs. Version-controlled prose,
        not a runtime-loaded script: the directive degrades to inline
        sequential rigor when no formal workflow runtime engages.
    *   ``fanout_n`` — the default panel width (concurrent verifiers / judges).
        Within :data:`_FANOUT_N_BOUNDS`. A user opt-in of ``true`` uses this
        default; an int opt-in overrides it (within the bounds, else raises).
    *   ``directive_template`` — the plain-prompt instruction rendered into the
        sub-agent's prompt. ``{n}`` is substituted with the resolved width. The
        text is SDK/API-portable (BLUEPRINT §2): identical whether the flow is
        driven by the terminal today or the Agent SDK later.
    """

    pattern: str
    fanout_n: int
    directive_template: str


#: The fan-out registry — opt-in, default-OFF, parallel to
#: ``SUBAGENT_BY_PHASE``. CONFORMANCE: every key here MUST also be a
#: ``SUBAGENT_BY_PHASE`` key (a fan-out can only apply to a dispatched
#: ``(role, phase)`` pair). Keyed on the canonical phase token so a task stored
#: with any accepted spelling resolves through ``normalize_phase``.
#:
#: review → adversarial-verify (one verifier per finding, default 3); planning
#: → judge-panel (N independent plans + a synthesis pass, default 3). Both the
#: author-role and reviewer-role reviewing phases carry the adversarial-verify
#: directive (a self-authored review and an assigned cold review both benefit).
#: bughunt → find-then-verify (N independent bug-finding passes, then a
#: verification pass that reproduces each candidate before it is reported).
FANOUT_BY_PHASE: dict[tuple[str, str], PhaseFanout] = {
    ("reviewer", "reviewing"): PhaseFanout(
        pattern="adversarial-verify",
        fanout_n=3,
        directive_template=(
            "FAN-OUT (adversarial-verify, N={n}): treat this review as an "
            "adversarial-verification panel of {n}. For each substantive "
            "finding, run an independent verification pass that actively tries "
            "to disprove it before you report it, so a false positive is caught "
            "by a sibling pass rather than shipped. If a parallel workflow "
            "runtime is available use it; otherwise perform the {n} passes "
            "inline and sequentially."
        ),
    ),
    ("author", "reviewing"): PhaseFanout(
        pattern="adversarial-verify",
        fanout_n=3,
        directive_template=(
            "FAN-OUT (adversarial-verify, N={n}): treat this review as an "
            "adversarial-verification panel of {n}. For each substantive "
            "finding, run an independent verification pass that actively tries "
            "to disprove it before you report it, so a false positive is caught "
            "by a sibling pass rather than shipped. If a parallel workflow "
            "runtime is available use it; otherwise perform the {n} passes "
            "inline and sequentially."
        ),
    ),
    ("author", "planning"): PhaseFanout(
        pattern="judge-panel",
        fanout_n=3,
        directive_template=(
            "FAN-OUT (judge-panel, N={n}): draft {n} independent candidate "
            "plans for this work, each exploring a distinct approach, then run "
            "a synthesis pass that judges them against one another and produces "
            "the single best plan (taking the strongest elements of each). If a "
            "parallel workflow runtime is available use it; otherwise produce "
            "the {n} candidates and the synthesis inline and sequentially."
        ),
    ),
    ("author", "bughunt"): PhaseFanout(
        pattern="find-then-verify",
        fanout_n=3,
        directive_template=(
            "FAN-OUT (find-then-verify, N={n}): run {n} independent bug-finding "
            "passes over the target, each hunting a distinct class of defect "
            "(logic errors, unhandled edge cases, races, stale assumptions). "
            "Then run a verification pass that reproduces each candidate before "
            "reporting it, so a false positive is caught by the verify pass "
            "rather than shipped. If a parallel workflow runtime is available "
            "use it; otherwise perform the {n} find passes and the verification "
            "inline and sequentially."
        ),
    ),
}


def fanout_for_phase(role: str, phase: str) -> "PhaseFanout | None":
    """Return the :class:`PhaseFanout` for a ``(role, phase)`` pair, or ``None``.

    ``phase`` is normalized so a task stored with a short-verb spelling
    (``review``/``plan``) resolves the same as the canonical gerund. ``None``
    means the pair has no registered fan-out — the single authority both
    dispatch routes consult. Mirrors :func:`subagent_for_phase`.
    """
    return FANOUT_BY_PHASE.get((role, normalize_phase(phase)))


def resolve_fanout_directive(role: str, phase: str, cfg: "AgentConfig") -> str:
    """Render the opt-in fan-out directive for a ``(role, phase)`` pair, or ``""``.

    The SINGLE chokepoint both the interactive (``loop_dispatch._task_to_dict``)
    and headless (``agents.prompt.build_system_context``) routes call. ``cfg`` is
    a resolved :class:`~teatree.config_agent.AgentConfig` passed by the
    loop-side caller, so ``core`` never imports UP into ``config_agent`` (tach).

    Default-OFF guarantee (the anti-vacuous spine): a pair with no
    ``[agent.phase_fanout]`` opt-in — the absent-key / ``False`` case — renders
    the **empty string**, so a dispatch is byte-identical to today until the
    user opts a pair in. An opt-in of ``True`` renders the registry's default
    ``fanout_n``; an int opt-in overrides it (must lie within
    :data:`_FANOUT_N_BOUNDS`); an int outside the bounds raises ``ValueError``
    (fail-loud) so a misconfiguration is seen, not silently clamped to a value
    the user did not write.
    """
    spec = fanout_for_phase(role, phase)
    if spec is None:
        return ""
    opt_in = _phase_fanout_opt_in(cfg.phase_fanout, role=role, phase=phase)
    resolved_n = _resolved_fanout_n(spec, opt_in=opt_in)
    if resolved_n is None:
        return ""
    return spec.directive_template.format(n=resolved_n)


def _phase_fanout_opt_in(phase_fanout: "dict[str, bool | int]", *, role: str, phase: str) -> "bool | int | None":
    """Look up the opt-in for a ``(role, phase)`` pair, tolerant of key spelling.

    The user writes ``[agent.phase_fanout]`` keys ``"role:phase"`` by hand, so a
    short-verb spelling (``"reviewer:review"``) must resolve the same as the
    canonical gerund (``"reviewer:reviewing"``) — mirroring
    :func:`fanout_for_phase`'s normalization on the registry side. Keys are
    normalized here (in ``core``, where :func:`normalize_phase` lives) rather
    than at parse time, so the platform-layer ``config_agent`` need not import UP
    into ``core``. The canonical key wins when both spellings are present.
    """
    canonical_key = f"{role}:{normalize_phase(phase)}"
    direct = phase_fanout.get(canonical_key)
    if direct is not None:
        return direct
    for raw_key, opt_in in phase_fanout.items():
        raw_role, _, raw_phase = raw_key.partition(":")
        if f"{raw_role}:{normalize_phase(raw_phase)}" == canonical_key:
            return opt_in
    return None


def _resolved_fanout_n(spec: PhaseFanout, *, opt_in: bool | int | None) -> int | None:
    """Resolve the effective ``N`` for an opt-in value, or ``None`` when disabled.

    ``None``/``False`` (absent or explicitly off) → ``None`` (no directive).
    ``True`` → the registry default ``spec.fanout_n``. An ``int`` → that value,
    which must lie within :data:`_FANOUT_N_BOUNDS` (out-of-range raises
    ``ValueError`` — fail-loud). ``bool`` is checked before ``int`` because
    ``bool`` is an ``int`` subclass.
    """
    if opt_in is None or opt_in is False:
        return None
    if opt_in is True:
        return spec.fanout_n
    low, high = _FANOUT_N_BOUNDS
    if not low <= opt_in <= high:
        message = f"Invalid phase_fanout N {opt_in!r}; valid range: {low}..{high} inclusive"
        raise ValueError(message)
    return opt_in


def normalize_phase(phase: str) -> str:
    """Return the canonical token for ``phase``.

    Accepts the short verbs the skills emit and the gerunds older code uses;
    case- and whitespace-insensitive. Unknown phases pass through lowered and
    stripped so visiting a free-form phase still records *something* rather
    than raising.
    """
    cleaned = phase.strip().lower()
    return _ALIAS_TO_CANONICAL.get(cleaned, cleaned)


def phase_spellings(phase: str) -> tuple[str, ...]:
    """Every stored spelling that normalizes to ``phase``'s canonical form.

    Lets a DB query match a phase regardless of which accepted spelling a
    row was stored with (``phase__in=phase_spellings(...)``) without a
    per-row ``normalize_phase`` call. For an unknown phase, returns just
    its own normalized form so callers still get an exact match.
    """
    canonical = normalize_phase(phase)
    return _PHASE_ALIASES.get(canonical, (canonical,))


def phase_transition(phase: str) -> str | None:
    """Return the FSM transition method name for ``phase``, or ``None``.

    ``None`` means the phase has no associated FSM transition (e.g. a
    free-form phase), so callers must not attempt to advance the FSM.
    """
    return _CANONICAL_TO_TRANSITION.get(normalize_phase(phase))
