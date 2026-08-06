"""Three readings of ``dev/.test_durations``, all ahead of the outage rather than during it (#4048).

The daily scheduled ``refresh-durations`` job is the only thing that keeps the
file current, and its failure mode is silence: the job is skipped, no refresh PR
is opened, and the file simply ages. Nothing downstream complains, because
pytest-split accepts a durations file of any size — it fills the gaps with the
average and produces a split that looks balanced and is not. The first visible
symptom is a per-test timeout on a shard, attributed to whichever PR was in
flight.

The same artifact answers the other half of that symptom. The sharded lane
raises the per-test ceiling because twelve shards on one runner cost more than
the tight local ceiling was sized for; the recorded durations are what say
whether a test is living off that raise. A raise nobody measures only moves the
cliff.

The third reading is the one that says the refresh has *stopped* rather than merely
fallen behind: how long ago a commit last touched the artifact. Coverage cannot say
it — a stalled pipeline and a pipeline still catching up read the same there — and it
is the only reading here that pages.

So all three signals are read off the committed artifact — the shape #3892 landed for
the CI OAuth pool, whose identical failure was a decaying value nobody was
watching until every merge stopped. None is a PR gate: staleness and drift are
not attributable to any diff, and a required check that reds every PR for
something no author caused is the very thing this epic exists to remove.
"""

import datetime as dt
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.quality.timeout_headroom import CeilingPressure

# The list is a prompt to act, not an inventory; past a handful it stops being read.
_NAMED_LIMIT = 5


def check_test_durations_coverage() -> bool:
    """WARN when too few test files are recorded for the shard split to be balanced.

    An advisory rather than a hard FAIL because of who consumes a FAIL. ``deploy/
    watchdog.sh`` execs ``t3 doctor check --json`` inside the stack every
    ``TEATREE_WATCHDOG_INTERVAL`` (300s), and DMs the owner every FAIL line that is
    not one of three deploy-sensitive tokens, re-keyed per day — so a hard FAIL here
    is a standing daily page for as long as the durations file is stale. Staleness is
    attributable to no actor and clears only when a refresh PR is merged, which can
    take weeks; a pager that fires nightly for something nobody caused is the failure
    mode this epic exists to remove, one surface over. The reading itself is
    unchanged — the same numbers and the same remedy print at every session start.

    ``MIN_FILE_COVERAGE`` is untouched; only the severity is. What a shortfall cannot
    say is whether anything is closing it — a pipeline catching up and a pipeline that
    has stopped both read as low coverage here. That is
    :func:`check_test_durations_freshness`'s question, and it is the one that pages.
    """
    from teatree.cli.doctor.service import DoctorService  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.quality import durations_coverage, durations_file  # noqa: PLC0415 — deferred: keeps CLI startup light

    repo = DoctorService.find_teatree_repo()
    if repo is None:
        return True

    try:
        coverage = durations_coverage.measure_durations_coverage(repo)
    except durations_file.DurationsUnreadableError as exc:
        typer.echo(f"FAIL  Test-shard durations: {exc}")
        typer.echo("      Restore the file from `ci/test-durations-refresh`, or from the last good commit.")
        return False

    if coverage is None:
        return True

    percent = f"{coverage.ratio:.1%}"
    if coverage.is_healthy:
        typer.echo(f"OK    Test-shard durations cover {percent} of the test files")
        return True

    typer.echo(
        f"WARN  Test-shard durations cover only {percent} of the test files "
        f"({coverage.covered_files}/{coverage.test_files}"
        f"{f', {coverage.orphan_keys} recorded key(s) name a deleted file' if coverage.orphan_keys else ''}) — "
        "pytest-split bin-packs every unrecorded test at the average, so the 12-way split is "
        "balanced only for the fraction it knows, and the shard that draws the slow ones reds "
        "whichever PR is in flight."
    )
    typer.echo(
        "      The daily scheduled run opens `ci/test-durations-refresh` with fresh durations — "
        "merge it. If no such PR exists, the refresh job is not running: check it on the latest "
        "`schedule` run of the CI workflow."
    )
    return True


def check_test_durations_freshness() -> bool:
    """FAIL when no commit has refreshed the durations artifact for ``MAX_REFRESH_AGE`` (#4130).

    The one page on this surface, and deliberately keyed on age rather than on the
    coverage figure its sibling reports. A shortfall clears only when a refresh PR
    merges, which can take weeks, so paging on it is a standing nightly alarm for
    something no actor caused — the failure this epic exists to remove. An age past
    the threshold is the opposite: it says the refresh itself has stopped, it is
    actionable by whoever reads it, and merging one refresh PR clears it for good.

    Silent when the age cannot be established (not a checkout, or a clone too shallow
    to hold the commit), and a WARN — never a FAIL, never silence — when git refuses
    to answer at all, so a check that has quietly stopped measuring is visible as
    unverified rather than indistinguishable from a healthy one.
    """
    from teatree.cli.doctor.service import DoctorService  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.quality import durations_freshness  # noqa: PLC0415 — deferred: keeps CLI startup light

    repo = DoctorService.find_teatree_repo()
    if repo is None:
        return True

    try:
        freshness = durations_freshness.measure_durations_freshness(repo, now=dt.datetime.now(tz=dt.UTC))
    except durations_freshness.DurationsHistoryUnreadableError as exc:
        typer.echo(f"WARN  Test-shard durations refresh age is UNVERIFIED: {exc}")
        return True
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Test-shard durations freshness check crashed: {exc.__class__.__name__}: {exc}")
        return True

    if freshness is None:
        return True

    landed = freshness.last_refreshed_at.date().isoformat()
    if not freshness.is_stale:
        typer.echo(f"OK    Test-shard durations were refreshed {freshness.age.days} days ago ({landed})")
        return True

    typer.echo(
        f"FAIL  Test-shard durations have not been refreshed in {freshness.age.days} days "
        f"(last landed {landed}, threshold {durations_freshness.MAX_REFRESH_AGE.days} days) — the daily "
        "refresh has stopped reaching `main`, so the 12-way split is drifting further from the suite "
        "it is splitting with nothing else to say so."
    )
    typer.echo(
        "      Either `ci/test-durations-refresh` is open and unmerged — merge it — or the scheduled "
        "run is not producing one: check the latest `schedule` run of the CI workflow, and its "
        "`refresh-durations` job in particular."
    )
    return False


def check_test_timeout_headroom() -> bool:
    """FAIL when a recorded duration exceeds the ceiling that applies to that test."""
    from teatree.cli.doctor.service import DoctorService  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.quality import durations_file, timeout_headroom  # noqa: PLC0415 — deferred: keeps CLI startup light

    repo = DoctorService.find_teatree_repo()
    if repo is None:
        return True

    try:
        report = timeout_headroom.measure_timeout_headroom(repo)
    except durations_file.DurationsUnreadableError:
        return True  # the coverage check above already FAILs on this file, with the remedy

    if report is None or not report.pressured:
        return True

    over = report.over_ceiling
    if over:
        typer.echo(
            f"FAIL  {len(over)} recorded test(s) exceed the timeout ceiling that applies to them — "
            "recorded, not predicted, so the shard that draws one reds whichever PR is in flight. "
            "Raising a test's own `@pytest.mark.timeout` to the measured cost clears this now (the "
            "ceiling is read from source); making the test faster is the better fix but leaves this "
            "standing until the next durations refresh lands, because the seconds are read from the "
            "committed file."
        )
    else:
        typer.echo(
            f"WARN  {len(report.pressured)} recorded test(s) run past {timeout_headroom.TIGHT_FRACTION:.0%} "
            "of their timeout ceiling — one contended shard from reddening a PR that did not touch them. "
            "Make each faster, or state its own `@pytest.mark.timeout` with the measurement."
        )
    _echo_pressure(report.pressured)
    if report.unresolved_ceilings:
        typer.echo(
            f"      ({report.unresolved_ceilings} file(s) name their ceiling rather than writing it, "
            "so their tests are not judged here.)"
        )
    return not over


def _echo_pressure(pressured: "tuple[CeilingPressure, ...]") -> None:
    for pressure in pressured[:_NAMED_LIMIT]:
        typer.echo(
            f"      {pressure.consumed:.0%} of {pressure.ceiling:g}s — {pressure.seconds:.1f}s  {pressure.node_id}"
        )
    if len(pressured) > _NAMED_LIMIT:
        typer.echo(f"      … and {len(pressured) - _NAMED_LIMIT} more")
