"""Frozen dataclasses for the eval harness."""

import dataclasses
from pathlib import Path
from typing import Any

#: Terminal reasons that mark a cap-truncated / aborted run — a run whose billed
#: cost does NOT match the clean billed identity (it paid a partial-or-cap cost).
#: A clean completion (``success``/``end_turn``/empty) is NOT in this set. The
#: canonical home: both the benchmark's clean-cell fit (``benchmark.py``) and the
#: pass@k aggregator (``pass_at_k.py``) classify against this one definition.
CAP_TERMINAL_REASONS: frozenset[str] = frozenset(
    {"budget_exceeded", "max_turns", "timeout", "error_max_turns", "error_max_budget_usd", "aborted"}
)


@dataclasses.dataclass(frozen=True)
class Matcher:
    """One assertion against captured tool calls.

    ``kind`` is ``"positive"`` (a matching tool call must exist) or
    ``"negative"`` (no matching tool call may exist). ``operator`` is
    ``"contains"`` (substring) or ``"~"`` (regex).
    """

    kind: str
    tool: str
    arg_path: str
    operator: str
    value: str


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


# An ``expect`` entry is either a single matcher or a disjunction of them.
ExpectItem = Matcher | AnyOf


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
    model: str = "claude-sonnet-4-6"
    max_output_tokens: int = 512


@dataclasses.dataclass(frozen=True)
class EvalSpec:
    """A single eval scenario loaded from YAML.

    ``agent_sections`` is the token-cost lever: when non-empty, only those
    ``## `` sections of ``agent_path`` (the SKILL.md) are sent as the system
    prompt instead of the whole file. A scenario pinning one rule sends that one
    rule, not all fifty — cutting the dominant per-scenario input cost. Empty
    (the default) sends the whole file, so existing scenarios are unchanged.
    """

    name: str
    scenario: str
    agent_path: str
    prompt: str
    matchers: tuple[ExpectItem, ...]
    source_path: Path
    model: str = "claude-sonnet-4-6"
    max_turns: int = 4
    tools: tuple[str, ...] = ("Bash",)
    judge: JudgeSpec | None = None
    agent_sections: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class EvalToolCall:
    name: str
    input: dict[str, Any]
    turn: int


#: Anthropic's fixed cache-pricing multipliers on input tokens, relative to the
#: model's base input rate: uncached input bills 1.00x, a 5-minute cache *write*
#: bills 1.25x, and a cache *read* bills 0.10x. They are a property of the API,
#: not of any model, so they are constants here — see :class:`TokenUsage`.
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


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
        return self.input + _CACHE_WRITE_MULTIPLIER * self.cache_creation + _CACHE_READ_MULTIPLIER * self.cache_read

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
