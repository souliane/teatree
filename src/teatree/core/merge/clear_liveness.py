"""Is an unconsumed merge authorisation a real stall, or did its PR already settle (#4250)?

A ``MergeClear`` with no ``MergeAudit`` was read as a stalled merge, and that
inference is wrong: a lost post-hook (process kill, DB lock, a rollback between
``execute_bound_merge`` and ``record_merge_and_advance``) leaves the PR merged on
the forge while the CLEAR stands unconsumed — a state
:meth:`~teatree.core.merge.ci_rollup.CodeHostQuery.pr_merge_state` already
documents. Six of six live alarm firings were PRs that had merged.

So the invariant here is inverted: **a stall requires positive evidence the PR is
OPEN.** Everything else — MERGED, CLOSED, an empty state, a 404, no ``gh``, no
token, offline — is not a stall and must not page. The forge read is INJECTED
(``read``), and the default reader is :func:`unverified_reader`, so a caller that
forgets to inject cannot page: fail-safe by construction rather than by care.

Backend-free on purpose — the caller supplies the reader
(``teatree.backends.loader.pr_open_state`` in production), so nothing here reaches
a forge and no test can accidentally do so.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models.merge_clear import MergeClear

#: PR/MR web URL → live forge state, the contract
#: :meth:`~teatree.core.models.pull_request.PullRequestQuerySet.reconcile_forge_states`
#: already injects (``PrOpenState`` is a ``StrEnum``, so a plain ``str`` satisfies it).
type PrStateReader = Callable[[str], str]

#: Oldest-first ceiling on forge reads per pass. Only rows that already passed every
#: local filter are probed, so a healthy box spends zero calls; the cap bounds the
#: worst case and what it drops is reported, never silently truncated.
PROBE_CAP = 10


class ClearLiveness(StrEnum):
    """What the forge says about the PR an unconsumed CLEAR authorises."""

    STALLED = "stalled"
    MERGED = "merged"
    ABANDONED = "abandoned"
    UNVERIFIED = "unverified"


def unverified_reader(pr_url: str) -> str:  # noqa: ARG001 — the fail-safe reader ignores its input by design
    """The default reader: every PR is UNKNOWN, so nothing classifies as a stall."""
    return PrOpenState.UNKNOWN


def clear_pr_url(clear: MergeClear) -> str:
    """*clear*'s PR web URL, or ``""`` when no real ``owner/repo`` resolves.

    ``MergeClear.slug`` is a workstream slug on most rows, so the repo comes from
    the canonical :func:`~teatree.core.merge.pr_slug_resolution.resolved_repo_slug`
    chain (own slug → ticket ``issue_url`` → clone origin). An empty return is the
    no-evidence short-circuit: :func:`~teatree.core.checking.build_pr_url` refuses a
    workstream or branch-shaped slug, so an unresolvable CLEAR costs no forge call.
    """
    from teatree.core.checking import build_pr_url  # noqa: PLC0415 — deferred: core.merge ↔ core.checking cycle
    from teatree.core.merge.pr_slug_resolution import resolved_repo_slug  # noqa: PLC0415 — deferred: same cycle

    return build_pr_url(slug=resolved_repo_slug(clear), pr_id=clear.pr_id, code_host=clear.host_kind)


def classify(clear: MergeClear, *, read: PrStateReader = unverified_reader) -> ClearLiveness:
    """Classify *clear* from the forge's own verdict on its PR.

    Only a literal OPEN is a stall. A reader that raises is UNVERIFIED for this row
    alone — per-row isolation, so one unreadable PR never decides the others.
    """
    url = clear_pr_url(clear)
    if not url:
        return ClearLiveness.UNVERIFIED
    try:
        state = read(url)
    except Exception:  # noqa: BLE001 — an unreadable forge is no evidence, never a verdict
        return ClearLiveness.UNVERIFIED
    if state == PrOpenState.OPEN:
        return ClearLiveness.STALLED
    if state == PrOpenState.MERGED:
        return ClearLiveness.MERGED
    if state == PrOpenState.CLOSED:
        return ClearLiveness.ABANDONED
    return ClearLiveness.UNVERIFIED


@dataclass(frozen=True, slots=True)
class LivenessProbe:
    """Per-row liveness for the probed prefix of a backlog, plus what the cap left unread."""

    verdicts: tuple[tuple[MergeClear, ClearLiveness], ...]
    unprobed: tuple[MergeClear, ...]

    def of(self, *wanted: ClearLiveness) -> list[MergeClear]:
        return [clear for clear, verdict in self.verdicts if verdict in wanted]


def probe(
    clears: Sequence[MergeClear],
    *,
    read: PrStateReader = unverified_reader,
    cap: int = PROBE_CAP,
) -> LivenessProbe:
    """Classify the oldest *cap* of *clears*, carrying the remainder as unprobed."""
    probed = clears[:cap]
    return LivenessProbe(
        verdicts=tuple((clear, classify(clear, read=read)) for clear in probed),
        unprobed=tuple(clears[cap:]),
    )
