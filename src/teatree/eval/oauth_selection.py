r"""Pick the FRESHEST eval OAuth account from a set of candidate tokens.

CI eval authenticates with ONE subscription OAuth token. When several accounts are
available (the ``EVAL_OAUTH_TOKENS`` CI secret, newline-separated), a run must never
land on a throttled/exhausted account — so this ranks the candidates by remaining
usage-window headroom and picks the one with the most room to spare.

THE PROBE. Each token is signed onto one tiny ``POST /v1/messages``
(:func:`teatree.llm.rate_limits.read_rate_limits`, ``is_oauth=True``); the response
carries the ``anthropic-ratelimit-unified-{5h,7d}-*`` headers — a 0.0-1.0 utilization
per window plus each window's reset — and a **429 still carries them**, so a throttled
account still yields a usable snapshot rather than a probe error. That is the cheapest
reliable per-token remaining-headroom signal the subscription/Code OAuth path exposes;
a genuine transport error (or an unexpected status) is the only thing that makes a
token *unreachable* (ineligible), never merely rate-limited.

THE SCORE mirrors :mod:`teatree.ci_oauth_switch` (the host-side sibling that points the
CI secret at the healthiest ``pass``-stored account). Ranking is lexicographic:

1. :attr:`CandidateHealth.binding_headroom` — the MINIMUM of the two windows' free
    fractions. A run is throttled by whichever window empties first, so a near-spent 5h
    window disqualifies an otherwise-rich weekly balance and vice versa.
2. :attr:`CandidateHealth.weighted_headroom` — a :data:`WEIGHT_5H` / :data:`WEIGHT_7D`
    blend of the two headrooms, breaking ties between equally-constrained accounts (the
    weekly window weighs more: a long benchmark outlasts several 5h windows but never the
    weekly one).
3. Input position — deterministic first on an exact tie / all-fresh set.

A 5h window whose reset falls at or before the run's start counts as FULLY free at
run start (the same reset projection :mod:`teatree.ci_oauth_switch` applies), so an
account rich by the time the run begins is not penalised for being spent right now.

TOKEN-FREE BY CONSTRUCTION. Candidates are identified by POSITION — ``token[1]``,
``token[2]``, … — and the result references the winner by :attr:`CandidateHealth.index`.
A token value never enters a snapshot, a score, a reason string, the returned
:class:`OAuthSelection`, or any log this module emits; the caller indexes back into its
own token list to fetch the winning value.

DJANGO-FREE. This runs in the CI step BEFORE any ``django.setup()``, so it imports only
the foundation-pure :mod:`teatree.llm.rate_limits`. The exhaustion thresholds and the
tie-break weights are replicated from their canonical homes
(:mod:`teatree.core.models.anthropic_token_usage`, :mod:`teatree.ci_oauth_switch`) and
pinned by a parity test rather than imported, so a drift is caught mechanically.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from teatree.llm.rate_limits import RateLimitProbeError, RateLimitReader, RateLimitSnapshot, read_rate_limits

# Replicated from teatree.core.models.anthropic_token_usage (the canonical routing
# exhaustion rule) to keep this selector Django-free; pinned by the parity test in
# tests/teatree_eval/test_oauth_selection.py.
UTILIZATION_5H_LIMIT = 0.95
UTILIZATION_7D_LIMIT = 0.99
REJECTED_STATUS = "rejected"

# Replicated from teatree.ci_oauth_switch's tie-break blend; parity-pinned.
WEIGHT_5H = 0.4
WEIGHT_7D = 0.6


class TokenProbeStatus(StrEnum):
    """A candidate's probe outcome — only :attr:`HEALTHY` is eligible to win."""

    HEALTHY = "healthy"
    EXHAUSTED = "exhausted"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class CandidateHealth:
    """One candidate token's probe outcome, keyed by POSITION — never the token value.

    ``index`` is the 0-based position in the input list (the caller's handle back to the
    token value); ``label`` is the human-safe ``token[N]`` id. ``headroom_*`` is each
    window's free fraction at the run's start (0.0 on a non-``HEALTHY`` candidate).
    """

    index: int
    label: str
    status: TokenProbeStatus
    organization_id: str = ""
    headroom_5h: float = 0.0
    headroom_7d: float = 0.0
    reason: str = ""

    @property
    def is_eligible(self) -> bool:
        return self.status is TokenProbeStatus.HEALTHY

    @property
    def binding_headroom(self) -> float:
        """The scarcer window's free fraction — what actually throttles the run."""
        return min(self.headroom_5h, self.headroom_7d)

    @property
    def weighted_headroom(self) -> float:
        """The blended headroom — tie-break between equally-constrained candidates."""
        return WEIGHT_5H * self.headroom_5h + WEIGHT_7D * self.headroom_7d


@dataclass(frozen=True)
class OAuthSelection:
    """Every probed candidate (input order) plus the eligible ones ranked best-first."""

    candidates: tuple[CandidateHealth, ...] = field(default_factory=tuple)
    ranked: tuple[CandidateHealth, ...] = field(default_factory=tuple)

    @property
    def winner(self) -> CandidateHealth | None:
        """The freshest eligible candidate, or ``None`` when none is eligible."""
        return self.ranked[0] if self.ranked else None

    @property
    def all_ineligible(self) -> bool:
        """Whether candidates were probed but NONE is eligible (the fail-loud/fallback case)."""
        return bool(self.candidates) and not self.ranked


def parse_tokens(raw: str) -> list[str]:
    """Split a newline-separated token blob into stripped, non-blank, deduped tokens.

    First-seen order is preserved so ``token[N]`` labels and the deterministic-first
    tie-break stay stable across runs with the same secret.
    """
    seen: dict[str, None] = {}
    for line in raw.splitlines():
        token = line.strip()
        if token:
            seen.setdefault(token, None)
    return list(seen)


def _headroom(utilization: float, reset: dt.datetime | None, run_start: dt.datetime) -> float:
    """A window's free fraction at *run_start* — fully free if it resets by then."""
    if reset is not None and reset <= run_start:
        return 1.0
    return max(0.0, 1.0 - utilization)


def _is_exhausted(snapshot: RateLimitSnapshot) -> bool:
    """The routing exhaustion rule applied to a probe snapshot."""
    return (
        snapshot.unified_5h_utilization >= UTILIZATION_5H_LIMIT
        or snapshot.unified_7d_utilization >= UTILIZATION_7D_LIMIT
        or snapshot.unified_7d_status == REJECTED_STATUS
    )


def _health_from_snapshot(
    index: int, label: str, snapshot: RateLimitSnapshot, run_start: dt.datetime
) -> CandidateHealth:
    if _is_exhausted(snapshot):
        reason = (
            f"exhausted — 5h {snapshot.unified_5h_utilization * 100:.0f}% used, "
            f"weekly {snapshot.unified_7d_utilization * 100:.0f}% used"
        )
        return CandidateHealth(
            index=index,
            label=label,
            status=TokenProbeStatus.EXHAUSTED,
            organization_id=snapshot.organization_id,
            reason=reason,
        )
    return CandidateHealth(
        index=index,
        label=label,
        status=TokenProbeStatus.HEALTHY,
        organization_id=snapshot.organization_id,
        headroom_5h=_headroom(snapshot.unified_5h_utilization, snapshot.unified_5h_reset, run_start),
        headroom_7d=_headroom(snapshot.unified_7d_utilization, snapshot.unified_7d_reset, run_start),
    )


def select_freshest(
    tokens: Sequence[str],
    *,
    reader: RateLimitReader | None = None,
    now: dt.datetime | None = None,
) -> OAuthSelection:
    """Probe every token in *tokens* and rank the eligible accounts by headroom.

    Each token is probed once through *reader* (default the real
    :func:`~teatree.llm.rate_limits.read_rate_limits`). A probe error marks the candidate
    :attr:`TokenProbeStatus.UNREACHABLE`; an exhausted snapshot marks it
    :attr:`TokenProbeStatus.EXHAUSTED`; both are ineligible. Eligible candidates rank
    lexicographically on binding then weighted headroom, deterministic first on a tie.
    """
    probe = reader or read_rate_limits
    run_start = now or dt.datetime.now(tz=dt.UTC)
    candidates: list[CandidateHealth] = []
    for index, token in enumerate(tokens):
        label = f"token[{index + 1}]"
        try:
            snapshot = probe(token, is_oauth=True)
        except RateLimitProbeError as exc:
            candidates.append(
                CandidateHealth(
                    index=index,
                    label=label,
                    status=TokenProbeStatus.UNREACHABLE,
                    reason=f"unreachable — {exc}",
                )
            )
            continue
        candidates.append(_health_from_snapshot(index, label, snapshot, run_start))
    eligible = [candidate for candidate in candidates if candidate.is_eligible]
    ranked = sorted(
        eligible,
        key=lambda candidate: (-candidate.binding_headroom, -candidate.weighted_headroom, candidate.index),
    )
    return OAuthSelection(candidates=tuple(candidates), ranked=tuple(ranked))


__all__ = [
    "REJECTED_STATUS",
    "UTILIZATION_5H_LIMIT",
    "UTILIZATION_7D_LIMIT",
    "WEIGHT_5H",
    "WEIGHT_7D",
    "CandidateHealth",
    "OAuthSelection",
    "TokenProbeStatus",
    "parse_tokens",
    "select_freshest",
]
