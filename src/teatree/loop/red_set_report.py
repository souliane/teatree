"""Compare failing REQUIRED checks ACROSS the open PR set (#4090).

Every other surface asks "what blocks THIS PR?" — the sweep decides per PR, the
aged-skip surfacer announces per ``(ref, reason)``, the status views render per
PR. A question about the SET is unanswerable that way: whether the board is
stuck depends on how the reds relate to each other and to ``main``, which no
per-PR view can see by construction.

This is the pure half. It takes one repo's red PRs plus ``main``'s own failing
check names and returns a :class:`RedSetReport` — main's verdict first, the
per-PR failing sets, what they share, what is unique to each, and one
:class:`SetVerdict`. No forge read, no mutation, no merge decision; the live
gathering and the one-shot announcement are
:mod:`teatree.loop.red_set_surface`.

``main-red`` is the one verdict this data EARNS: main's own failing checks
intersected with what the set is failing is direct evidence no merge ordering
clears the board. ``disjoint-reds`` deliberately asserts nothing — a mutual
block and wholly unrelated failures reach it through identical records, so the
note names both readings and picks neither. Distinguishing them needs
dependency evidence (a "this PR fixes check X" signal, failing-job-log content,
or the merge-and-re-run experiment) that no surface records, which is also why
``independent`` gains no new rung: making it reachable for n≥2 would mean
claiming exactly the dependency this module cannot observe.

The two indeterminate rungs are load-bearing. A run judged against a base the
branch has fallen behind carries an UNKNOWN verdict (#4063), and an unreadable
``main`` cannot be assumed green — either one refuses to read a red the board
inherits as a property of the set.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "MIN_SET_SIZE",
    "PrRedRecord",
    "RedSetReport",
    "SetVerdict",
    "analyse_red_set",
]

#: Below this there is no SET to reason about — a lone red PR's failure is its
#: own, and every per-PR surface already says so.
MIN_SET_SIZE = 2


class SetVerdict(Enum):
    """The one line the whole report exists to produce."""

    MAIN_RED = "main-red"
    MAIN_INDETERMINATE = "main-indeterminate"
    SHARED_CAUSE = "shared-cause"
    DISJOINT_REDS = "disjoint-reds"
    INDEPENDENT = "independent"


@dataclass(frozen=True, slots=True)
class PrRedRecord:
    """One open PR's failing REQUIRED checks, and whether its run judged the current base."""

    ref: str
    failing: frozenset[str]
    base_current: bool = True
    url: str = ""


@dataclass(frozen=True, slots=True)
class RedSetReport:
    """The read-only answer to a question about the SET, not about any one PR.

    ``main_failing`` is ``None`` when ``main``'s checks could not be read —
    distinct from an empty set (``main`` is green), because assuming green over
    an unread ``main`` is exactly how a cycle gets claimed for a red the whole
    board inherits. ``inherited`` is the part of ``main``'s red the set is also
    failing; ``shared`` is the intersection across every red PR.
    """

    slug: str
    main_failing: frozenset[str] | None
    records: tuple[PrRedRecord, ...]
    verdict: SetVerdict
    shared: frozenset[str]
    inherited: frozenset[str]

    def exclusive(self) -> tuple[tuple[str, frozenset[str]], ...]:
        """Per-PR failing checks NO other PR in the set is failing.

        The linear-size form of the pairwise differences: for a set of two it IS
        the pairwise difference, and for a larger set it stays the decision-relevant
        half ("which failure is only mine?") without an N² render.
        """
        return tuple(
            (record.ref, record.failing - _union(other for other in self.records if other.ref != record.ref))
            for record in self.records
        )

    def signature(self) -> str:
        """A stable digest of the CLAIM — the idempotency key the announcement is keyed on.

        Order-independent and content-addressed, so a set that stays stalled
        signs identically forever (announced once) while any change to the
        membership, the failing names, the base freshness or the verdict is a
        new claim that is announced again.

        The main term is ``inherited``, not ``main_failing``: measured over 8
        consecutive main commits a FIXED red set took 4 distinct signatures on
        the unscoped set and 1 on this one, because incidental reds no PR is
        failing (``deploy``, ``refresh-durations``, a shard) flip constantly and
        are not part of the claim. ``inherited`` is also already scoped to the
        branch-protection-REQUIRED contexts, since it intersects with each
        record's ``failing``.
        """
        parts = [self.slug, self.verdict.value, _names(self.inherited) if self.main_failing is not None else "?"]
        parts += [f"{record.ref}|{_names(record.failing)}|{int(record.base_current)}" for record in self.records]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def render(self) -> str:
        """The report, main's verdict first — plain lines, safe as Slack mrkdwn."""
        lines = [self._main_line(), f"verdict: {self.verdict.value} — {self._verdict_note()}"]
        lines += [_pr_line(record) for record in self.records]
        lines.append(f"shared by every PR: {_names(self.shared) or 'none'}")
        lines += [f"only {ref}: {_names(names) or 'none'}" for ref, names in self.exclusive()]
        return "\n".join(lines)

    def _main_line(self) -> str:
        if self.main_failing is None:
            return f"main ({self.slug}): check-runs unreadable"
        if self.inherited:
            return f"main ({self.slug}): red on {_names(self.inherited)}"
        if self.main_failing:
            return f"main ({self.slug}): red on {_names(self.main_failing)}, none of them in this red set"
        return f"main ({self.slug}): green"

    def _verdict_note(self) -> str:
        if self.verdict is SetVerdict.MAIN_RED:
            return f"every PR here inherits main's {_names(self.inherited)} — no merge ordering clears it"
        if self.verdict is SetVerdict.MAIN_INDETERMINATE:
            return "main's checks could not be read, so a cycle cannot be claimed over it"
        if self.verdict is SetVerdict.SHARED_CAUSE:
            return f"{len(self.records)} PRs fail {_names(self.shared) or 'a common check'} — one cause, one fix"
        if self.verdict is SetVerdict.DISJOINT_REDS:
            return (
                f"{len(self.records)} PRs, no failing check in common, every run judged the current base, "
                "and main is green on everything this set fails. This surface sees no dependency evidence "
                "either way — consistent with a mutual block (each red only because another's unmerged fix "
                "is missing) and equally with unrelated failures"
            )
        return "no shared cause across the set — each red is that PR's own"


def analyse_red_set(
    *,
    slug: str,
    main_failing: frozenset[str] | None,
    records: Iterable[PrRedRecord],
) -> RedSetReport | None:
    """The set-level verdict for one repo, or ``None`` when no open PR is red.

    *main_failing* is the default branch head's failing check names, or ``None``
    when they could not be read. Records with no failing check are not part of
    the red set and are dropped.
    """
    red = tuple(sorted((record for record in records if record.failing), key=lambda record: record.ref))
    if not red:
        return None
    inherited = (main_failing or frozenset()) & _union(red)
    return RedSetReport(
        slug=slug,
        main_failing=main_failing,
        records=red,
        verdict=_verdict(red, main_failing=main_failing, inherited=inherited),
        shared=_intersection(red),
        inherited=inherited,
    )


def _verdict(
    records: tuple[PrRedRecord, ...],
    *,
    main_failing: frozenset[str] | None,
    inherited: frozenset[str],
) -> SetVerdict:
    if inherited:
        return SetVerdict.MAIN_RED
    if len(records) < MIN_SET_SIZE:
        return SetVerdict.INDEPENDENT
    if _any_pair_overlaps(records):
        return SetVerdict.SHARED_CAUSE
    if not all(record.base_current for record in records):
        return SetVerdict.INDEPENDENT
    if main_failing is None:
        return SetVerdict.MAIN_INDETERMINATE
    return SetVerdict.DISJOINT_REDS


def _any_pair_overlaps(records: tuple[PrRedRecord, ...]) -> bool:
    seen: set[str] = set()
    for record in records:
        if seen & record.failing:
            return True
        seen |= record.failing
    return False


def _union(records: Iterable[PrRedRecord]) -> frozenset[str]:
    return frozenset().union(*(record.failing for record in records))


def _intersection(records: tuple[PrRedRecord, ...]) -> frozenset[str]:
    return frozenset(records[0].failing).intersection(*(record.failing for record in records[1:]))


def _names(names: Iterable[str]) -> str:
    return ", ".join(sorted(names))


def _pr_line(record: PrRedRecord) -> str:
    base = "current" if record.base_current else "stale"
    label = f"[{record.ref}]({record.url})" if record.url else record.ref
    return f"- {label} failing: {_names(record.failing)} (base: {base})"
