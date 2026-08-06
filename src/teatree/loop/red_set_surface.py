"""Gather the open red PR set from the tick's own signals and announce it ONCE (#4090).

The live half of :mod:`teatree.loop.red_set_report`. It reads the sweep's emitted
signals rather than re-listing PRs, so the report is computed from the exact
snapshot the merge decision was made on and costs no second forge read per PR —
the sweep already classified each PR's failing REQUIRED checks through the shared
§17.4.3 classifier, and stamps them on the signal. The only live read is one
paginated ``main`` check-run query per repo, which is the single boolean that
reframes the whole board.

``main-red`` over a set of two or more is the one claim this data earns, so it is
DM'd — keyed on the set's own signature, so a permanently stalled board is
announced once instead of producing the same quiet line forever. A lone PR
inheriting main's red is not a SET claim and is already visible per PR, so it
stays log-only, as does every other verdict. Nothing here mutates or decides a
merge.

Every failure is swallowed: an unreachable forge, a missing overlay, a broken DM
transport or a malformed payload must degrade to a quiet tick, never abort one.
"""

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable
from typing import TypedDict, cast

from teatree.loop.red_set_report import MIN_SET_SIZE, PrRedRecord, RedSetReport, SetVerdict, analyse_red_set
from teatree.loop.scanners.base import ScanSignal, SignalPayload
from teatree.loop.scanners.pr_sweep_types import GREEN_TERMINAL_CONCLUSIONS
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

__all__ = ["record_red_set"]

_SWEEP_PREFIX = "pr_sweep."

#: The branch every red PR is cut from. Matches the hardcoded base
#: ``GhPrApiClient.main_check_failed`` already reads, so the sweep's uv-audit
#: fallback and this report can never disagree about which commit is "main".
_MAIN_BRANCH = "main"

#: Page size under ``--paginate``. Main carried 86 check-runs at one sampled
#: commit against a 30-run default page, so this is the request count today's
#: board costs — never the ceiling, which is what ``--paginate`` is for.
_CHECK_RUNS_PAGE = 100

type MainChecks = Callable[..., frozenset[str] | None]
type CycleNotifier = Callable[..., None]


class _CheckRun(TypedDict, total=False):
    """One entry of a ``check_runs`` page body."""

    name: object
    status: object
    conclusion: object


def record_red_set(
    signals: Iterable[ScanSignal],
    *,
    main_checks: MainChecks | None = None,
    notify: CycleNotifier | None = None,
) -> list[RedSetReport]:
    """Fold this tick's sweep signals into one set-level report per repo.

    Returns the reports (empty on the common path — no open PR is red).
    """
    try:
        grouped = _group(signals)
        if not grouped:
            return []
        probe = main_checks or _default_main_checks
        return [
            report
            for (slug, overlay), records in sorted(grouped.items())
            if (report := _report_for(slug=slug, overlay=overlay, records=records, probe=probe, notify=notify))
        ]
    except Exception:
        logger.exception("red_set: cross-PR report failed")
        return []


def _report_for(
    *,
    slug: str,
    overlay: str,
    records: list[PrRedRecord],
    probe: MainChecks,
    notify: CycleNotifier | None,
) -> RedSetReport | None:
    report = analyse_red_set(
        slug=slug, main_failing=_main_failing(slug=slug, overlay=overlay, probe=probe), records=records
    )
    if report is None:
        return None
    logger.info(
        "red_set %s (%s): verdict=%s over %d red PR(s)",
        slug,
        overlay,
        report.verdict.value,
        len(report.records),
    )
    if report.verdict is SetVerdict.MAIN_RED and len(report.records) >= MIN_SET_SIZE:
        _announce(report, notify or _default_notify)
    return report


def _main_failing(*, slug: str, overlay: str, probe: MainChecks) -> frozenset[str] | None:
    """``main``'s failing check names, or ``None`` when they could not be established.

    A probe that RAISES is the same condition as one that reports it cannot tell,
    so it degrades to ``None`` — never to an empty set, which would read as a
    green ``main`` and hide a red the whole board inherits.
    """
    try:
        return probe(slug=slug, overlay=overlay)
    except Exception:
        logger.exception("red_set: main check-runs probe failed for %s — treating as indeterminate", slug)
        return None


def _announce(report: RedSetReport, notify: CycleNotifier) -> None:
    try:
        notify(text=report.render(), idempotency_key=f"pr_sweep_red_set:{report.signature()}")
    except Exception:
        logger.exception("red_set: failed to announce the set-level claim on %s", report.slug)


def _group(signals: Iterable[ScanSignal]) -> dict[tuple[str, str], list[PrRedRecord]]:
    grouped: dict[tuple[str, str], list[PrRedRecord]] = {}
    for signal in signals:
        if not signal.kind.startswith(_SWEEP_PREFIX):
            continue
        if (decoded := _decode(signal.payload)) is not None:
            key, record = decoded
            grouped.setdefault(key, []).append(record)
    return grouped


def _decode(payload: SignalPayload) -> tuple[tuple[str, str], PrRedRecord] | None:
    """One sweep signal as a ``((slug, overlay), record)`` pair, or ``None`` when it is not red.

    Grouped by the fully-qualified ``(slug, overlay)`` key: two overlays watching
    a repo of the same name read it under different tokens and must never have
    their red sets merged into one claim.
    """
    slug = str(payload.get("slug") or "")
    pr_id = payload.get("pr_id")
    failing = payload.get("failing_required")
    if not slug or not isinstance(pr_id, int) or not isinstance(failing, list | tuple) or not failing:
        return None
    record = PrRedRecord(
        ref=f"{slug}#{pr_id}",
        failing=frozenset(str(name) for name in failing),
        base_current=bool(payload.get("base_current", True)),
        url=str(payload.get("url") or ""),
    )
    return (slug, str(payload.get("overlay") or "")), record


def _default_main_checks(*, slug: str, overlay: str) -> frozenset[str] | None:
    """The names of ``main``'s completed, non-green check-runs, read across ALL pages.

    Unpaginated, this read saw only the first 30 of main's check-runs, so which
    required contexts it reported depended on the order the API happened to
    return them — five of the seven required contexts were absent from page 1 at
    one sampled commit. A verdict keyed on a content-addressed signature has to
    be repeatable, so the read is paginated.

    ``--slurp`` is required rather than preferred: bare ``--paginate`` emits
    concatenated JSON documents that :func:`json.loads` rejects, and ``gh``
    refuses ``--slurp`` together with ``--jq``, so the projection moves into
    :func:`_failing_check_names`.

    ``None`` on ANY read failure (no ``gh``, non-zero exit, unparsable body), so
    an unreadable ``main`` can never be mistaken for a green one — which also
    covers a ``gh`` older than 2.53, where ``--slurp`` exits non-zero.
    """
    gh = shutil.which("gh") or "gh"
    argv = [
        gh,
        "api",
        "--paginate",
        "--slurp",
        f"repos/{slug}/commits/{_MAIN_BRANCH}/check-runs?per_page={_CHECK_RUNS_PAGE}",
    ]
    token = _github_token(overlay)
    try:
        result = run_allowed_to_fail(
            argv, expected_codes=None, env={**os.environ, "GH_TOKEN": token} if token else None
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        logger.warning("red_set: could not read main check-runs for %s (rc=%d)", slug, result.returncode)
        return None
    return _failing_check_names(result.stdout)


def _failing_check_names(out: str) -> frozenset[str] | None:
    """Classify the slurped pages, or ``None`` when they carry no evidence.

    ``--slurp`` yields one list of page bodies, each ``{"total_count", "check_runs"}``,
    so the runs are flattened across pages. A payload with NO check-run at all is
    indeterminate, not green — nothing has reported on that commit, the same
    "cannot tell" the non-zero exit above returns — and that now covers GitHub's
    real empty shape, one page carrying an empty list.
    """
    try:
        pages = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(pages, list):
        return None
    runs = [
        cast("_CheckRun", run)
        for page in pages
        if isinstance(page, dict)
        for run in page.get("check_runs", [])
        if isinstance(run, dict)
    ]
    if not runs:
        return None
    return frozenset(_check_name(run) for run in runs if _is_failing(run)) - {""}


def _check_name(run: "_CheckRun") -> str:
    return str(run.get("name") or "")


def _is_failing(run: "_CheckRun") -> bool:
    """A check-run that has FINISHED on a non-green conclusion.

    A still-running check is not a failure, and an absent conclusion on a
    completed run counts as one — the conservative side, since an over-reported
    ``main`` red only ever refuses the cycle claim.
    """
    if str(run.get("status") or "").upper() != "COMPLETED":
        return False
    return str(run.get("conclusion") or "").upper() not in GREEN_TERMINAL_CONCLUSIONS


def _github_token(overlay: str) -> str:
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: overlay registry at call time

    try:
        return get_overlay(overlay or None).config.get_github_token() or ""
    except Exception:
        logger.exception("red_set: no GitHub token for overlay %r — falling back to ambient gh auth", overlay)
        return ""


def _default_notify(*, text: str, idempotency_key: str) -> None:
    from teatree.core.modelkit.notify_policy import NotifyAudience  # noqa: PLC0415 — deferred: integration import
    from teatree.core.notify import NotifyKind  # noqa: PLC0415 — deferred: integration import
    from teatree.messaging import notify_with_fallback  # noqa: PLC0415 — deferred: integration import

    notify_with_fallback(
        text=text,
        kind=NotifyKind.INFO,
        idempotency_key=idempotency_key,
        audience=NotifyAudience.OWNER_ESCALATION,
    )
