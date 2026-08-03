"""FAIL when the shard split is running blind on a stale ``dev/.test_durations`` (#4048).

The daily scheduled ``refresh-durations`` job is the only thing that keeps the
file current, and its failure mode is silence: the job is skipped, no refresh PR
is opened, and the file simply ages. Nothing downstream complains, because
pytest-split accepts a durations file of any size — it fills the gaps with the
average and produces a split that looks balanced and is not. The first visible
symptom is a per-test timeout on a shard, attributed to whichever PR was in
flight.

So the signal has to be read off the committed artifact, ahead of the outage
rather than during it — the shape #3892 landed for the CI OAuth pool, whose
identical failure was a decaying value nobody was watching until every merge
stopped. It is deliberately NOT a PR gate: staleness is not attributable to any
diff, and a required check that reds every PR for something no author caused is
the very thing this epic exists to remove.
"""

import typer


def check_test_durations_coverage() -> bool:
    """FAIL when too few test files are recorded for the shard split to be balanced."""
    from teatree.cli.doctor.service import DoctorService  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.quality import durations_coverage  # noqa: PLC0415 — deferred: keeps CLI startup light

    repo = DoctorService.find_teatree_repo()
    if repo is None:
        return True

    try:
        coverage = durations_coverage.measure_durations_coverage(repo)
    except durations_coverage.DurationsUnreadableError as exc:
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
        f"FAIL  Test-shard durations cover only {percent} of the test files "
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
    return False
