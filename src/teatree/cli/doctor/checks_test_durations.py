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

So both signals are read off the committed artifact — the shape #3892 landed for
the CI OAuth pool, whose identical failure was a decaying value nobody was
watching until every merge stopped. Neither is a PR gate: staleness and drift are
not attributable to any diff, and a required check that reds every PR for
something no author caused is the very thing this epic exists to remove.

The third reading is the one that pages (#4130). Both readings above are advisory
by design, so a refresh pipeline that stops produces no FAIL at all and the whole
arrangement reads green while it rots. Age is what distinguishes a file the running
pipeline has not caught up to yet from one no pipeline is behind — see
:mod:`teatree.quality.durations_freshness` for why the window is what it is.
"""

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

    ``MIN_FILE_COVERAGE`` is untouched; only the severity is. What pages instead is
    :func:`check_test_durations_freshness`, which keys on the age of the last refresh
    rather than on the coverage number — so the known-stale-but-improving state this
    WARN tolerates cannot re-page, while a refresh pipeline that has stopped does.
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
    """FAIL when no refresh of the durations file has landed within the window.

    The one alarm on this artifact that pages. Its remedy is the ``refresh-durations``
    job rather than anything in the tree, which is why it names that job and the branch
    it opens inside the FAIL line itself: ``deploy/watchdog.sh`` DMs the FAIL line only,
    so a remedy on a continuation line never reaches the owner.
    """
    from teatree.cli.doctor.service import DoctorService  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.quality import durations_freshness  # noqa: PLC0415 — deferred: keeps CLI startup light

    repo = DoctorService.find_teatree_repo()
    if repo is None:
        return True

    freshness = durations_freshness.measure_durations_freshness(repo)
    if freshness is None or not freshness.is_stale:
        return True

    typer.echo(
        f"FAIL  No `dev/.test_durations` refresh has landed in {freshness.age_days} days "
        f"(last: {freshness.landed_at.date().isoformat()}, window "
        f"{durations_freshness.MAX_REFRESH_AGE_DAYS} days) — the daily scheduled "
        "`refresh-durations` job has stopped producing, so the 12-way split is bin-packing "
        "from a file that no longer describes the suite. Read that job on the latest "
        "`schedule` run of the CI workflow, and merge the `ci/test-durations-refresh` PR."
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
            "Raising a test's own `@pytest.mark.timeout` to the measured cost clears this at once "
            "(the ceiling is read from source); making the test faster clears it only once the next "
            "durations refresh records the new cost."
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
