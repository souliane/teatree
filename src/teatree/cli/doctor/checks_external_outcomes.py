"""Reconciliation checks whose numbers come from the FORGE, not from teatree (#4506).

Every sibling check in :mod:`~teatree.cli.doctor.checks_reconciliation` reads the
control DB — park rows, spend, loop anchors, task attempts. All of them stay green
through a window in which nothing reached ``main``, because tasks reporting
``completed`` is bookkeeping, not evidence that anything shipped.

The two checks here take at least one number from the forge, so a factory whose
internal state is entirely green cannot satisfy them by itself:
``external_output_vs_internal_success`` compares internal SUCCESS attempts against
merged pull requests; ``merged_without_verdict`` takes its DENOMINATOR from the
forge (which PRs actually merged) and its numerator from teatree (which of them a
``ReviewVerdict`` vouches for).

Both share one snapshot per cadence, so the pair costs one forge read however often
the watchdog invokes ``t3 doctor``. Django-free at import, like every sibling.
"""

import datetime as dt
from collections.abc import Mapping
from typing import TYPE_CHECKING

from teatree.cli.doctor.reconciliation_finding import ReconciliationFinding, _alarm, _degraded, _now, _ok, _unavailable

if TYPE_CHECKING:
    from teatree.types import RawAPIDict

#: Internal successes in the external-outcome window above which zero forge merges is
#: a disagreement rather than a quiet week.
MIN_INTERNAL_SUCCESSES_FOR_OUTCOME = 25
#: Merged PRs carrying no recorded verdict above which the review gate is alarmed. A
#: count floor, not ``>= 1``: one hand-merged or bot PR is normal and would otherwise
#: pin the finding red forever, training the reader to ignore it.
MAX_UNVOUCHED_MERGES = 3
#: How many unvouched refs a finding names before collapsing into "and N more" — the
#: message is digested into the watchdog's idempotency key, so it stays bounded.
_NAMED_REF_CAP = 5


def _pr_ref_pairs(refs: object) -> list[tuple[str, int]]:
    """``(slug, number)`` per persisted ref; a malformed entry is dropped, not guessed."""
    pairs: list[tuple[str, int]] = []
    if not isinstance(refs, list):
        return pairs
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        fields: RawAPIDict = {str(key): value for key, value in ref.items()}
        slug, number = fields.get("slug"), fields.get("number")
        if isinstance(slug, str) and isinstance(number, int) and not isinstance(number, bool):
            pairs.append((slug, number))
    return pairs


def _external_window(now: dt.datetime | None) -> tuple[dt.datetime, dt.timedelta]:
    from teatree.core.factory.external_outcomes import (  # noqa: PLC0415 — deferred: ORM-backed, Django-free at CLI load
        DEFAULT_EXTERNAL_WINDOW_DAYS,
    )

    return _now(now), dt.timedelta(days=DEFAULT_EXTERNAL_WINDOW_DAYS)


def _external_snapshot(now: dt.datetime) -> tuple[int, list[tuple[str, int]], str]:
    """``(merged_pr_count, merged_pr_refs, unavailable_reason)`` for the trailing window.

    Serves a snapshot younger than the TTL, so the two checks below share ONE forge
    read per cadence however often the watchdog invokes doctor.
    """
    from teatree.core.factory.external_outcomes import (  # noqa: PLC0415 — deferred: ORM-backed, Django-free at CLI load
        ExternalOutcomeStatus,
        refresh_if_stale,
    )

    snapshot = refresh_if_stale(now=now)
    if snapshot.status != ExternalOutcomeStatus.OK.value:
        reason = f"no code host or no declared repos for this overlay (status `{snapshot.status}`)"
        return 0, [], reason
    return snapshot.merged_pr_count, _pr_ref_pairs(snapshot.merged_pr_refs), ""


def _check_external_output_vs_internal_success(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when internal successes pile up in the window while the forge shows zero merges.

    Internal: ``TaskAttempt`` rows with ``outcome=SUCCESS`` in the trailing external
    window. External: the merged-PR count the FORGE reports for the overlay's own
    repos. Thresholds: :data:`MIN_INTERNAL_SUCCESSES_FOR_OUTCOME` internal successes
    against zero merges.

    A failed or absent forge read is DEGRADED, never ``_ok`` and never the alarm — an
    unread forge must not present as either healthy or empty.
    """
    check_id = "external_output_vs_internal_success"
    try:
        from teatree.core.models import TaskAttempt  # noqa: PLC0415 — ORM import needs the app registry

        moment, window = _external_window(now)
        internal = TaskAttempt.objects.filter(
            outcome=TaskAttempt.Outcome.SUCCESS,
            started_at__gte=moment - window,
        ).count()
        merged, _refs, unavailable = _external_snapshot(moment)
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if unavailable:
        return _unavailable(check_id, unavailable)
    days = window.days
    if merged > 0 or internal < MIN_INTERNAL_SUCCESSES_FOR_OUTCOME:
        return _ok(check_id, f"{internal} internal successes / {merged} forge merges in {days}d")
    return _alarm(
        check_id,
        f"External-outcome alarm: `{internal}` task attempts recorded SUCCESS in the last "
        f"`{days}d` while the forge reports `{merged}` pull requests merged. Internal bookkeeping "
        f"is green and nothing reached the default branch — grade the factory on the second number. "
        f"Check the merge loop with `t3 <overlay> ticket list --in-flight` and `t3 doctor`.",
    )


def _check_merged_without_verdict(now: dt.datetime | None = None) -> ReconciliationFinding:
    """ALARM when merges the FORGE reports carry no recorded ``ReviewVerdict``.

    The denominator is forge-side (which PRs actually merged), the numerator is
    teatree's own (which of them a verdict vouches for), so no amount of internal
    success can satisfy it. Threshold: :data:`MAX_UNVOUCHED_MERGES`.

    This is the "a reviewing task reached completed having persisted no verdict"
    signature read from the outside: a review that recorded nothing is
    indistinguishable downstream from one that approved, but it is plainly visible
    against the list of PRs that merged.
    """
    check_id = "merged_without_verdict"
    try:
        from teatree.core.models import ReviewVerdict  # noqa: PLC0415 — ORM import needs the app registry

        moment, window = _external_window(now)
        merged, refs, unavailable = _external_snapshot(moment)
        unvouched = [
            f"`{slug}#{number}`" for slug, number in refs if not ReviewVerdict.objects.for_pr(slug, number).exists()
        ]
    except Exception as exc:  # noqa: BLE001 — a reconciliation read must never crash the doctor run
        return _degraded(check_id, exc)
    if unavailable:
        return _unavailable(check_id, unavailable)
    vouched = merged - len(unvouched)
    if len(unvouched) < MAX_UNVOUCHED_MERGES:
        return _ok(check_id, f"{vouched}/{merged} forge merges carry a recorded verdict")
    named = ", ".join(unvouched[:_NAMED_REF_CAP])
    tail = "" if len(unvouched) <= _NAMED_REF_CAP else f" and {len(unvouched) - _NAMED_REF_CAP} more"
    return _alarm(
        check_id,
        f"Merged-without-verdict alarm: the forge reports `{merged}` pull request(s) merged in the "
        f"last `{window.days}d` and only `{vouched}` carry a recorded ReviewVerdict — "
        f"`{len(unvouched)}` merged unvouched: {named}{tail}. "
        f"A review that recorded no verdict is indistinguishable downstream from one that approved.",
    )


#: The forge-read half of the reconciliation ledger, in a stable report order.
EXTERNAL_CHECKS: tuple = (
    _check_external_output_vs_internal_success,
    _check_merged_without_verdict,
)
