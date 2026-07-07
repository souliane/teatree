"""Frozen dataclasses for the eval harness."""

import dataclasses
from pathlib import Path
from typing import Any

from teatree.pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER

#: Terminal reasons that mark a cap-truncated / aborted run — a run whose billed
#: cost does NOT match the clean billed identity (it paid a partial-or-cap cost).
#: A clean completion (``success``/``end_turn``/empty) is NOT in this set. The
#: canonical home: both the benchmark's clean-cell fit (``benchmark.py``) and the
#: pass@k aggregator (``pass_at_k.py``) classify against this one definition.
CAP_TERMINAL_REASONS: frozenset[str] = frozenset(
    {"budget_exceeded", "max_turns", "timeout", "error_max_turns", "error_max_budget_usd", "aborted"}
)

#: GENEROUS default per-scenario turn budget for a scenario that declares no
#: ``max_turns`` of its own. The old default of ``4`` force-FAILed multi-step /
#: sub-agent-spawning scenarios (delegate/spawn trajectories need many turns), so
#: a truncated run measured the cap, not behaviour. Raised generously; a scenario
#: still declares its own lower value, and the lane reads an optional global
#: override (:func:`teatree.eval.api_runner.resolve_max_turns_override`,
#: ``T3_EVAL_MAX_TURNS``) that otherwise defers to this per-scenario budget.
DEFAULT_MAX_TURNS = 30

#: Minimum turn budget the clean-room lane grants a scenario regardless of a
#: lower per-scenario ``max_turns`` declaration. Many catalog scenarios declare a
#: very tight ``max_turns: 3`` calibrated to an earlier era where the model
#: emitted the decisive tool call on turn 1. Current Claude models ORIENT before
#: acting (inspect the repo, verify the premise) and frequently emit the correct
#: matched action EARLY but keep going for several turns — and a cap-truncated run
#: force-FAILs the gate even when every matcher passed (#2192,
#: :attr:`ScenarioResult.passed` returns ``False`` on a ``max_turns`` terminal
#: reason). The result was a SYSTEMIC clean-room collapse: the agent did the right
#: thing first, then tripped the cap and the right behaviour was nullified. The
#: floor grants orient + act + stop headroom so a correct early action is not
#: erased by trailing exploration. It NEVER lowers a higher per-scenario value
#: (``max(declared, floor)``) and applies to the clean-room lane only — the
#: under_load lane keeps its own turn/watchdog calibration. The matchers are
#: untouched, so the teeth are unchanged: a WRONG action still grades RED — a
#: higher floor only lets a run whose matchers ALL pass terminate naturally
#: instead of being truncated mid-trajectory.
#:
#: Recalibrated 6 → 15 (#2627 follow-up): a fresh metered single-trial run of the
#: full suite found 16 clean-room scenarios that did the right thing (every
#: matcher green) yet still cap-FAILed at the 6-turn floor — the model needed up
#: to ~12 decisive assistant turns of orient → act → verify → stop before
#: terminating on its own. The earlier floor of 6 recovered the act-on-turn-1
#: cluster but not the act-then-verify-then-stop cluster the current models
#: exhibit. 15 covers the observed worst case (~12 turns) with headroom; because
#: a model stops on its own once done, the floor only bites a run still going AT
#: the cap, so the higher value costs nothing on a scenario that finishes early.
CLEAN_ROOM_MIN_TURNS = 15

#: The default eval lane. A clean-room scenario loads ONE skill into an empty
#: context; the catalog's existing specs all default to it, so their runs are
#: byte-identical. ``UNDER_LOAD_LANE`` is the behavioural-drift lane that loads
#: the FULL skill bundle plus an injected polluted ``context_preamble`` to
#: reproduce the instruction-following drift a real session exhibits.
CLEAN_ROOM_LANE = "clean_room"
UNDER_LOAD_LANE = "under_load"
PERMITTED_LANES: frozenset[str] = frozenset({CLEAN_ROOM_LANE, UNDER_LOAD_LANE})


@dataclasses.dataclass(frozen=True)
class Matcher:
    """One assertion against captured tool calls.

    ``kind`` is ``"positive"`` (a matching tool call must exist) or
    ``"negative"`` (no matching tool call may exist). ``operator`` is
    ``"contains"`` (substring) or ``"~"`` (regex).

    A negative matcher may carry an optional ORDER guard (the ``guard_*`` fields,
    parsed from the YAML ``before_first`` sibling key): when set, the forbidden
    call reds the run ONLY when it occurs at a turn STRICTLY BEFORE the first
    call matching the guard (or when no guard call exists at all). This expresses
    "X must not happen BEFORE Y" — e.g. "no MR-diff read before the overlay skill
    loads" — where a plain order-agnostic negative wrongly reds the correct
    load-skill-THEN-read trajectory. Empty guard fields (the default) leave the
    negative order-agnostic, so every existing matcher is byte-identical.
    """

    kind: str
    tool: str
    arg_path: str
    operator: str
    value: str
    guard_tool: str = ""
    guard_arg_path: str = ""
    guard_operator: str = ""
    guard_value: str = ""

    @property
    def has_order_guard(self) -> bool:
        return bool(self.guard_tool)


@dataclasses.dataclass(frozen=True)
class AnyOf:
    """A disjunction of positive matchers — passes when ANY alternative holds.

    Pins a rule that a documented set of equally-valid actions satisfies
    (e.g. "background the long op via a ``Task`` dispatch OR a Bash call
    with ``run_in_background: true``"), so a compliant response taking
    either branch stays green. Restricted to positive alternatives: a
    disjunction of negatives is just one wider negative regex, and mixing
    a forbidden branch into an "any-of" reads ambiguously.
    """

    alternatives: tuple[Matcher, ...]


@dataclasses.dataclass(frozen=True)
class FinalStateMatcher:
    """An assertion about the run's END STATE — its final assistant message.

    The tool-call matchers (:class:`Matcher`) look across the whole trajectory;
    this one pins the agent's TERMINAL answer (the last ``text_blocks`` entry).
    It carries no ``tool``/``arg_path`` because there is exactly one subject — the
    final message — so a scenario declares only the ``operator`` (``contains`` /
    ``~``) and the ``value`` to match against it.
    """

    operator: str
    value: str


# An ``expect`` entry is a single tool-call matcher, a disjunction of them, or an
# assertion about the run's final assistant message (the end state).
ExpectItem = Matcher | AnyOf | FinalStateMatcher

#: The SINGLE SOURCE OF TRUTH for the matcher grammar, read by every place that has
#: to agree on it: the loader compiles ``_OP_PATTERN`` from ``MATCHER_OPERATORS`` and
#: keys its ``expect``-entry dispatch on ``MATCHER_KINDS``; the grader
#: (``report._dispatch``) branches on the same operators; and the dream eval
#: synthesizer prompt (``llm_eval_proposer``) enumerates BOTH so it can never tell
#: the model to emit a shape the loader rejects. Hand-duplicating the grammar into
#: the prompt is exactly what dropped every derived candidate in #2646 — these
#: constants make a future operator/kind change touch one place and fan out, instead
#: of silently re-opening the drift.
#:
#: ``MATCHER_OPERATORS`` are the operator tokens an ``op "value"`` expression may use
#: (``contains`` substring / ``~`` regex). ``MATCHER_KINDS`` are the four
#: ``expect``-entry kinds, ordered as the synthesizer prompt teaches them.
MATCHER_OPERATORS: tuple[str, ...] = ("contains", "~")
MATCHER_KINDS: tuple[str, ...] = ("tool_call", "no_tool_call_matching", "any_of", "final_state")

#: Case aliases mapping a tool name's lowercase form to its canonical name. The
#: single source of truth so the grader (``report._canonicalize_tool``) and the
#: metered runner's toolset restriction (``api_runner.compute_disallowed_tools``)
#: canonicalize identically.
#:
#: ``task`` -> ``Agent`` because the bundled ``claude`` CLI names the SUB-AGENT
#: SPAWN tool ``Agent``, NOT ``Task`` — there is no ``Task`` *spawn* tool (the CLI
#: does register a ``Task`` tool, but ``Task`` resolves to no known *spawn* tool
#: and delegation silently never happens, the same toolset-drift class as the
#: removed ``MultiEdit`` in #2627). Scenarios and their
#: matchers historically wrote ``Task`` (the user-facing/UI name), so a declared
#: ``tools: [..., Task]`` produced a ``--tools`` allowlist with the phantom
#: ``Task`` AND pushed the REAL ``Agent`` onto the ``--disallowedTools`` denylist —
#: the delegation scenarios could therefore NEVER call a spawn tool, and their
#: ``tool_call: Task`` matchers could never match the emitted ``Agent`` call.
#: Aliasing ``Task`` -> ``Agent`` here makes BOTH sides agree on the real CLI tool:
#: the allowlist exposes ``Agent`` (no longer disallowed) and the matcher's
#: expected tool canonicalizes to the ``Agent`` name the model actually emits.
#: The exact-key lowercase lookup never touches the distinct team-mode task-list
#: built-ins (``TaskCreate`` / ``TaskUpdate`` / ``TaskList`` / ``TaskGet`` / …) —
#: their lowercase forms (``taskcreate`` etc.) are not the key ``task``.
_TOOL_ALIASES = {"bash": "Bash", "task": "Agent"}


def canonicalize_tool(name: str) -> str:
    """Canonicalize a tool *name* (``bash`` -> ``Bash``, ``Task`` -> ``Agent``, else passthrough).

    The single normalization both the grader and the metered-lane toolset
    restriction apply, so a matcher's tool and a declared ``tools`` entry are
    compared in the same canonical space. ``Task`` maps to the bundled CLI's real
    sub-agent spawn tool ``Agent`` (the CLI registers no ``Task`` *spawn* tool); the
    exact-key match leaves ``TaskCreate`` / ``TaskUpdate`` / ``TaskList`` (the
    team-mode task-list built-ins) untouched.
    """
    return _TOOL_ALIASES.get(name.lower(), name)


@dataclasses.dataclass(frozen=True)
class JudgeSpec:
    """Opt-in LLM-judge grading config for a scenario.

    Present only when a scenario's pass/fail is not cleanly matcher-gradeable
    (e.g. "the explanation is faithful to the diff", "the tone is non-blaming").
    A judge model reads the captured transcript and the ``rubric`` and returns
    a PASS/FAIL verdict. ``model`` is the judge tier (defaults to the Sonnet run
    tier) and ``max_output_tokens`` caps the judge's reply — both cost controls.
    """

    rubric: str
    model: str = "claude-sonnet-5"
    max_output_tokens: int = 512


@dataclasses.dataclass(frozen=True)
class GateEvent:
    """One production-hook lifecycle event captured from a ``production_hooks`` run.

    Synthesized from the SDK ``HookEventMessage`` (only ``hook_response`` — a hook
    that COMPLETED; ``hook_started`` is lifecycle noise the mapper drops).
    ``outcome``/``output_snippet`` come from the CLI's ``hook_response`` ``data``.

    It is a REPORT-ANNOTATION + fail-loud channel, never a per-scenario pass
    condition: :attr:`is_stop_block` tells the report whether a #807-class Stop
    *block* carried a pass (rendered ``pass (gate-assisted)``), and the runner's
    zero-hook-events fail-loud uses the PRESENCE of any hook event to prove the
    shipped hook chain registered under the eval wiring.
    """

    hook_event_name: str
    outcome: str
    output_snippet: str

    @property
    def is_block(self) -> bool:
        """Whether this event's outcome/output signals a hook BLOCK decision."""
        return "block" in f"{self.outcome}\n{self.output_snippet}".lower()

    @property
    def is_stop_block(self) -> bool:
        """A #807-class Stop-gate block — the gate that carries a gate-assisted pass."""
        return self.hook_event_name == "Stop" and self.is_block


@dataclasses.dataclass(frozen=True)
class EvalSpec:
    """A single eval scenario loaded from YAML.

    ``agent_sections`` is the token-cost lever: when non-empty, only those
    ``## `` sections of ``agent_path`` (the SKILL.md) are sent as the system
    prompt instead of the whole file. A scenario pinning one rule sends that one
    rule, not all fifty — cutting the dominant per-scenario input cost. Empty
    (the default) sends the whole file, so existing scenarios are unchanged.

    ``lane`` selects the harness mode. ``"clean_room"`` (the default, every
    existing spec) loads one skill into an empty context. ``"under_load"``
    reproduces real-session drift: the FULL skill bundle is the system prompt and
    ``context_preamble`` (an 8k-20k-token polluted prefix) is folded into the
    user prompt text. The SDK ``query`` is user-turns-only, so the pollution
    lives in the prompt text — never as pre-seeded assistant/tool-result turns.

    Model resolution is by ABSTRACT TIER, not a concrete model id. A scenario
    declares ``tier`` (``frontier`` / ``balanced`` / ``cheap``) or ``phase`` (a
    teatree FSM phase name) instead of pinning a model; the runner resolves
    these through the single :data:`teatree.agents.model_tiering.TIER_MODELS`
    constant. ``model`` is the escape hatch for a deliberate concrete-id pin and
    wins when set; ``model`` is ``""`` (unset) on a tier/phase scenario. The
    resolution precedence, highest first: ``model`` > ``tier`` > ``phase`` >
    :data:`teatree.agents.model_tiering.DEFAULT_TIER`.
    """

    name: str
    scenario: str
    agent_path: str
    prompt: str
    matchers: tuple[ExpectItem, ...]
    source_path: Path
    #: A deliberate concrete-model-id pin (the escape hatch). ``""`` (the default)
    #: means unset — resolution falls through to ``tier`` / ``phase`` / the
    #: default tier. A non-empty value is an explicit ``model[@effort]`` pin that
    #: WINS over ``tier``/``phase``.
    model: str = ""
    #: Abstract model tier (``frontier`` / ``balanced`` / ``cheap``). Resolved to a
    #: concrete model id through ``TIER_MODELS``. Wins over ``phase`` and the
    #: default; loses to an explicit ``model``.
    tier: str = ""
    #: A teatree FSM phase name (``planning`` / ``coding`` / …). Resolved to its
    #: tier via ``DEFAULT_PHASE_MODELS`` then to a model. Loses to ``model`` and
    #: ``tier``; wins over the default tier.
    phase: str = ""
    max_turns: int = DEFAULT_MAX_TURNS
    tools: tuple[str, ...] = ("Bash",)
    #: Skill names to widen the clean-room's simulated Skill-tool catalog with, on
    #: top of whatever the CLI discovers on its own. Empty (the default) leaves the
    #: SDK ``skills``/``plugins`` options untouched, so every existing scenario is
    #: byte-identical to before this field existed. A scenario whose prompt
    #: references a skill name core does not itself ship — a placeholder overlay's
    #: workspace/legal-entity skill, a companion language bible (``ac-django`` /
    #: ``ac-python``), or the review skill named without a leading slash — declares
    #: the referenced names here so the runner registers the eval-only fixture
    #: plugin (``evals/fixtures/skill_catalog``) and lists exactly this set: the
    #: agent's own "only invoke a listed name" refusal rule is real and must stay
    #: intact, so the fix widens what is listed rather than bypassing the rule. See
    #: ``teatree.eval.api_runner.build_sdk_options``.
    available_skills: tuple[str, ...] = ()
    #: Opt-in throwaway sandbox fixture (``""`` = the neutral empty cwd). A scenario
    #: whose prompt presupposes a working tree (staged changes, commits to squash)
    #: declares ``fixture: git_repo`` so the runner provisions a real repo whose
    #: state matches the prompt — otherwise the agent's first ``git`` returns nothing
    #: and it investigates the mismatch instead of firing the command. See
    #: :func:`teatree.eval.git_fixture.provision_git_fixture`.
    fixture: str = ""
    #: Opt-in inert CLI stubs (``t3`` / ``gh`` / ``glab``). A single-action probe
    #: whose CORRECT command is ``t3 <overlay> notify send …`` (or a forge diff)
    #: runs in a sandbox with no wired CLI, so that command ERRORS and the agent
    #: wanders into a ``max_turns`` cap-taint even though the matcher already
    #: matched the correct call. Declaring ``cli_stubs: [t3]`` prepends a throwaway
    #: ``bin/`` of inert success-printing stubs to the child's ``PATH`` so the
    #: command succeeds and the agent stops. The stubs print but hold no state, so
    #: the matchers grade the CALL (negatives keep full teeth). A SEPARATE lever
    #: from :attr:`fixture` — the two compose. Empty (the default) leaves ``PATH``
    #: untouched, so every existing scenario is byte-identical. See
    #: :mod:`teatree.eval.cli_stub_fixture`.
    cli_stubs: tuple[str, ...] = ()
    #: Register the shipped teatree plugin (``hooks/hooks.json``) into the SDK
    #: child so the scenario measures the model+hook SYSTEM that ships, not the raw
    #: model with hooks stripped. The clean-room personal-context isolation
    #: (``setting_sources=[]``, redirected HOME, empty user-level ``settings``
    #: hooks) is unchanged — only the shipped PLUGIN hook chain is added, plus the
    #: sandbox-local redirection of the loop/hook state roots so the #807 Stop gate
    #: sees a fresh owner-less registry and fires. Empty (the default) leaves the
    #: SDK ``plugins``/``include_hook_events`` untouched, so every existing scenario
    #: is byte-identical to before this field existed. See
    #: :func:`teatree.eval.api_runner.build_sdk_options`.
    production_hooks: bool = False
    judge: JudgeSpec | None = None
    agent_sections: tuple[str, ...] = ()
    lane: str = CLEAN_ROOM_LANE
    context_preamble: str = ""
    #: Per-scenario USD budget ceiling, overriding the run-level
    #: ``max_budget_usd``. ``None`` defers to the run default. A delegation scenario
    #: whose CORRECT trajectory dispatches a sub-agent that runs a legitimate TDD
    #: cycle (worktree provision + red test + implement + green + commit) costs more
    #: than a single-turn scenario; capping it at the shared default would truncate
    #: the correct behaviour rather than measure it (a #2192 cap-tainted trial reds
    #: the whole scenario). A scenario raises this to FIT the legitimate sub-agent
    #: work — the matchers are unchanged, so the cap relief never weakens the teeth.
    max_budget_usd: float | None = None
    #: Per-scenario wall-clock watchdog (seconds), overriding the lane default. Same
    #: rationale as :attr:`max_budget_usd`: a sub-agent TDD cycle takes longer than a
    #: single-turn probe, so a delegation scenario raises its watchdog to fit the
    #: legitimate work rather than time out the correct trajectory. ``None`` defers
    #: to the lane default.
    watchdog_seconds: float | None = None


@dataclasses.dataclass(frozen=True)
class EvalToolCall:
    name: str
    input: dict[str, Any]
    turn: int


@dataclasses.dataclass(frozen=True)
class TokenUsage:
    """One run's token usage, split by cache class, with billed-input derivations.

    The four fields mirror the SDK ``ResultMessage.usage`` keys: ``input``
    (uncached input), ``cache_creation`` (tokens written cold into the cache —
    a 1.25x write), ``cache_read`` (tokens served from cache — a 0.10x read),
    and ``output``. They default to ``0`` so a subscription/offline/capped run
    with no usage yields an all-zero instance rather than a missing one.

    The derived properties answer the cache-cost questions price-table-free:
    ``cache_hit_rate`` (how much input was served warm), ``cold_write_tokens``
    (the share that did NOT benefit from cache), and ``effective_billed_input``
    (the API's billed-input regressor under Anthropic's fixed multipliers).
    ``__add__`` lets trials and cells sum.
    """

    input: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output: int = 0

    @property
    def total_input(self) -> int:
        return self.input + self.cache_creation + self.cache_read

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of input served from cache (``cache_read / total_input``); 0.0 when no input."""
        total = self.total_input
        return self.cache_read / total if total else 0.0

    @property
    def cold_write_tokens(self) -> int:
        """Tokens written cold into the cache — input that did NOT benefit from a prior read."""
        return self.cache_creation

    @property
    def effective_billed_input(self) -> float:
        """The API's billed-input regressor: ``input + 1.25*cache_creation + 0.10*cache_read``."""
        return self.input + CACHE_WRITE_MULTIPLIER * self.cache_creation + CACHE_READ_MULTIPLIER * self.cache_read

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input=self.input + other.input,
            cache_creation=self.cache_creation + other.cache_creation,
            cache_read=self.cache_read + other.cache_read,
            output=self.output + other.output,
        )


@dataclasses.dataclass(frozen=True)
class EvalRun:
    """Captured output of one eval-runner invocation against a spec."""

    spec_name: str
    tool_calls: tuple[EvalToolCall, ...]
    text_blocks: tuple[str, ...]
    terminal_reason: str
    is_error: bool
    raw_stdout: str
    raw_stderr: str
    cost_usd: float = 0.0
    usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    billed_model: str | None = None
    #: Whether the REQUESTED main model was substituted (a fallback). ``True`` =
    #: the requested model is ABSENT from ``model_usage`` (Claude Code's haiku
    #: auxiliary sitting beside the requested model is NORMAL, not a fallback);
    #: ``False`` = present; ``None`` = unobservable (subscription/offline run).
    fell_back: bool | None = None
    #: Metered cost of the requested MAIN model (the comparison number) and the
    #: AUXILIARY background (Claude Code's haiku), split from per-model
    #: ``model_usage.costUSD``. ``0.0`` on a non-metered/unobservable run.
    main_cost_usd: float = 0.0
    aux_cost_usd: float = 0.0
    #: Token usage of the MAIN model vs the AUXILIARY background, split from the
    #: per-model ``model_usage`` token counts (all-zero when unobservable).
    main_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    aux_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    #: Production-hook lifecycle events captured on a ``production_hooks`` run
    #: (empty on every other run, incl. every recorded-transcript replay — those
    #: carry no hook stream). Additive: the report reads it to annotate a
    #: gate-assisted pass; grading never consults it.
    gate_events: tuple[GateEvent, ...] = ()
