"""The reclaim-disk safety boundary: only the zero-risk Docker reclaims run.

The whole point of ``reclaim_disk`` is that it is *impossible* for it to nuke an
in-use stack or trigger an ``-a``-class blast. These tests pin that boundary by
asserting on the EXACT argv each step passes to docker (the docker binary is the
unstoppable external — mocked at :func:`teatree.docker.reclaim._run_prune`), not
on incidental output. Reverting the safety filter (adding ``-a`` to the image
prune, or routing an active stack's volume into the reclaim set) turns these
red.
"""

from subprocess import CompletedProcess

import pytest

from teatree.docker import reclaim
from teatree.docker.venue import DockerVenue
from teatree.utils.run import TimeoutExpired


@pytest.fixture(autouse=True)
def _reachable_venue(monkeypatch):
    """Default every test to a venue that can act, so only the venue tests probe docker."""
    monkeypatch.setattr(reclaim, "docker_venue", lambda: DockerVenue(reachable=True))


class _FakePrune:
    """Records every argv ``reclaim_disk`` would pass to docker; returns a stub size."""

    def __init__(self, *, reclaimed: str = "0B") -> None:
        self.calls: list[list[str]] = []
        self._reclaimed = reclaimed

    def __call__(self, argv: list[str]) -> reclaim.PruneOutcome:
        self.calls.append(list(argv))
        return reclaim.PruneOutcome(reclaimed=self._reclaimed, bytes_reclaimed=0)

    @property
    def flat_argv(self) -> list[str]:
        return [tok for call in self.calls for tok in call]


def _carries_all_flag(call: list[str]) -> bool:
    """True if *call* passes the docker "all" blast flag in any form.

    Catches ``--all``, a bare ``-a``, and a combined short cluster (``-af`` /
    ``-fa``). On ``image`` / ``volume`` / ``system`` prune the all-flag reaps
    tagged application images / named volumes / everything — the blast this
    command must never emit.
    """
    for token in call[1:]:
        if token == "--all":
            return True
        if token.startswith("-") and not token.startswith("--") and "a" in token:
            return True
    return False


def test_reclaim_set_is_exactly_the_three_safe_prunes(monkeypatch):
    fake = _FakePrune()
    monkeypatch.setattr(reclaim, "_run_prune", fake)

    reclaim.reclaim_disk()

    assert fake.calls == [
        ["docker", "builder", "prune", "-af"],
        ["docker", "image", "prune", "-f"],
        ["docker", "volume", "prune", "-f"],
    ]


def test_image_prune_never_passes_dash_a(monkeypatch):
    """``-a`` / ``--all`` / ``-af`` on the image prune reaps tagged application images — banned."""
    fake = _FakePrune()
    monkeypatch.setattr(reclaim, "_run_prune", fake)

    reclaim.reclaim_disk()

    image_call = next(call for call in fake.calls if call[:3] == ["docker", "image", "prune"])
    assert not _carries_all_flag(image_call), f"image prune must never carry the all-flag: {image_call}"


def test_never_emits_a_dash_a_or_system_prune_anywhere(monkeypatch):
    """No image/volume/system step may carry the ``-a``/``-af``/``--all`` blast flag.

    ``builder prune -af`` is the one exception: there ``-a`` means "all build
    cache" (rebuildable), not "all images" — that is the safe full-cache reclaim.
    """
    fake = _FakePrune()
    monkeypatch.setattr(reclaim, "_run_prune", fake)

    reclaim.reclaim_disk()

    for call in fake.calls:
        assert "system" not in call, f"system prune is never safe: {call}"
        if call[1] == "builder":
            continue
        assert not _carries_all_flag(call), f"all-flag blast forbidden on {call[:3]}"
    assert ["docker", "builder", "prune", "-af"] in fake.calls  # the one safe -af reclaim


def test_volume_prune_is_unreferenced_only_never_force_all(monkeypatch):
    """``docker volume prune -f`` removes ONLY unreferenced volumes — an attached.

    DB volume backing a live worktree carries a container reference and survives.
    ``-a``/``--all`` would also remove named-but-unattached volumes (a worktree DB
    whose stack is merely stopped), so it is banned here.
    """
    fake = _FakePrune()
    monkeypatch.setattr(reclaim, "_run_prune", fake)

    reclaim.reclaim_disk()

    volume_call = next(call for call in fake.calls if call[:3] == ["docker", "volume", "prune"])
    assert volume_call == ["docker", "volume", "prune", "-f"]


def test_dry_run_runs_nothing_destructive(monkeypatch):
    fake = _FakePrune()
    monkeypatch.setattr(reclaim, "_run_prune", fake)

    report = reclaim.reclaim_disk(dry_run=True)

    assert fake.calls == []
    assert report.dry_run is True
    assert {step.argv[1] for step in report.planned} == {"builder", "image", "volume"}


def test_report_totals_each_step_and_the_sum(monkeypatch):
    sizes = iter(["1.0GB", "200MB", "512MB"])

    def fake_run(argv: list[str]) -> reclaim.PruneOutcome:
        raw = next(sizes)
        return reclaim.PruneOutcome(reclaimed=raw, bytes_reclaimed=reclaim._parse_size(raw))

    monkeypatch.setattr(reclaim, "_run_prune", fake_run)

    report = reclaim.reclaim_disk()

    assert len(report.steps) == 3
    assert report.total_bytes == reclaim._parse_size("1.0GB") + reclaim._parse_size("200MB") + reclaim._parse_size(
        "512MB"
    )
    assert report.total_human  # a non-empty human-readable total


def test_parse_size_handles_docker_size_strings():
    assert reclaim._parse_size("0B") == 0
    assert reclaim._parse_size("3.333GB") == int(3.333 * 1000**3)
    assert reclaim._parse_size("512MB") == 512 * 1000**2
    assert reclaim._parse_size("1.5kB") == int(1.5 * 1000)
    assert reclaim._parse_size("nonsense") == 0


def test_parse_reclaimed_reads_both_docker_summary_shapes():
    """``image``/``volume`` prune emit ``Total reclaimed space: X``; ``builder`` emits ``Total: X``."""
    image_stdout = "deleted: sha256:abc\n\nTotal reclaimed space: 3.333GB\n"
    builder_stdout = "id\ttrue\t16kB\n\nTotal:\t22.45GB\n"
    assert reclaim._extract_reclaimed(image_stdout) == "3.333GB"
    assert reclaim._extract_reclaimed(builder_stdout) == "22.45GB"
    assert reclaim._extract_reclaimed("nothing here") == "0B"


def test_human_bytes_scales_and_caps_at_petabytes():
    assert reclaim._human_bytes(0) == "0B"
    assert reclaim._human_bytes(512) == "512B"
    assert reclaim._human_bytes(1_500) == "1.5kB"
    assert reclaim._human_bytes(2 * 1000**3) == "2.0GB"
    assert reclaim._human_bytes(5 * 1000**5) == "5.0PB"
    assert reclaim._human_bytes(9999 * 1000**5).endswith("PB")  # never overflows past the top unit


def test_run_prune_parses_real_docker_stdout(monkeypatch):
    def fake_run(cmd, **_):
        return CompletedProcess(args=cmd, returncode=0, stdout="Total reclaimed space: 2.0GB\n", stderr="")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", fake_run)
    outcome = reclaim._run_prune(["docker", "image", "prune", "-f"])
    assert outcome.reclaimed == "2.0GB"
    assert outcome.bytes_reclaimed == 2 * 1000**3


def test_run_prune_returns_zero_when_docker_binary_missing(monkeypatch):
    def boom(cmd, **_):
        msg = "docker"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", boom)
    outcome = reclaim._run_prune(["docker", "volume", "prune", "-f"])
    assert outcome == reclaim.PruneOutcome(reclaimed="0B", bytes_reclaimed=0)


def test_run_prune_records_the_reason_on_nonzero_exit(monkeypatch):
    """Docker answered and refused: reclaim nothing, but say why.

    This previously asserted a bare ``PruneOutcome("0B", 0)`` — pinning the
    silent-no-op as intended behaviour. The reason is what lets the caller tell
    "refused" apart from "nothing to reclaim".
    """

    def failed(cmd, **_):
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="daemon down")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", failed)
    outcome = reclaim._run_prune(["docker", "image", "prune", "-f"])

    assert outcome.bytes_reclaimed == 0
    assert outcome.failure == "daemon down"


def test_run_prune_reports_a_timeout_rather_than_swallowing_it(monkeypatch):
    """A timed-out prune did not finish; claiming a clean ``0B`` asserts more than is known."""

    def slow(cmd, **_):
        raise TimeoutExpired(cmd, 1)

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", slow)
    outcome = reclaim._run_prune(["docker", "builder", "prune", "-af"])

    assert outcome.bytes_reclaimed == 0
    assert outcome.failure is not None
    assert "timed out" in outcome.failure


def test_run_prune_falls_back_to_exit_status_when_stderr_is_empty(monkeypatch):
    def silent_failure(cmd, **_):
        return CompletedProcess(args=cmd, returncode=2, stdout="", stderr="   ")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", silent_failure)
    outcome = reclaim._run_prune(["docker", "volume", "prune", "-f"])

    assert outcome.failure == "exit status 2"


def test_reclaim_disk_end_to_end_with_real_run_prune_seam(monkeypatch):
    """The full ``reclaim_disk`` flow with only the subprocess boundary mocked."""
    outputs = {
        "builder": "Total:\t1.0GB\n",
        "image": "Total reclaimed space: 0B\n",
        "volume": "Total reclaimed space: 512MB\n",
    }

    def fake_run(cmd, **_):
        key = cmd[1]
        return CompletedProcess(args=cmd, returncode=0, stdout=outputs[key], stderr="")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", fake_run)
    report = reclaim.reclaim_disk()
    assert [step.outcome.reclaimed for step in report.steps] == ["1.0GB", "0B", "512MB"]
    assert report.total_bytes == 1000**3 + 512 * 1000**2


@pytest.mark.parametrize("dry_run", [True, False])
def test_reclaim_report_total_human_is_always_a_string(dry_run, monkeypatch):
    monkeypatch.setattr(reclaim, "_run_prune", lambda argv: reclaim.PruneOutcome("0B", 0))
    report = reclaim.reclaim_disk(dry_run=dry_run)
    assert isinstance(report.total_human, str)


# --- Fail-loud: a reachable-but-erroring docker must never read as a 0B success ---
#
# The tolerance this module documents is for docker being ABSENT (CI sandboxes,
# hermetic tests). A docker that answers and refuses is a different case: the
# operator asked for a reclaim, none happened, and the disk is still full. These
# pin the distinction — absence stays silent, failure is surfaced.


def test_daemon_unreachable_surfaces_as_report_failures(monkeypatch):
    """The observed defect: the containerized ``t3`` worker has no docker socket.

    Every prune errored with "failed to connect to the docker API", yet the
    command exited 0 printing ``Total reclaimed: 0B`` — indistinguishable from
    "nothing to reclaim" — while the disk stayed full.
    """
    unreachable = "failed to connect to the docker API at unix:///var/run/docker.sock"

    def no_socket(cmd, **_):
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr=unreachable)

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", no_socket)
    report = reclaim.reclaim_disk()

    assert len(report.failures) == 3, "every step failed; every failure must be reported"
    assert all(unreachable in detail for _, detail in report.failures)
    assert unreachable in report.failure_summary()


def test_every_step_still_runs_when_an_earlier_one_fails(monkeypatch):
    """A failing step must not forfeit the reclaim the remaining steps can still do.

    Aborting on the first error is the wrong loud: under disk pressure the
    partial reclaim is exactly what the operator needs.
    """

    def first_fails(cmd, **_):
        if cmd[1] == "builder":
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="cache locked")
        return CompletedProcess(args=cmd, returncode=0, stdout="Total reclaimed space: 512MB\n", stderr="")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", first_fails)
    report = reclaim.reclaim_disk()

    assert len(report.steps) == 3
    assert report.total_bytes == 2 * 512 * 10**6  # the two survivors still reclaimed
    assert [label for label, _ in report.failures] == ["build cache"]


def test_missing_docker_binary_stays_tolerated(monkeypatch):
    """No docker at all never raises and is never a per-STEP failure — the CI-sandbox tolerance.

    Tolerated means the library does not crash, not that the run reads as a
    completed reclaim: the venue probe answers that question separately, and
    ``test_absent_docker_cli_is_venue_blocked_not_a_clean_zero`` pins it.
    """

    def boom(cmd, **_):
        msg = "docker"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", boom)
    report = reclaim.reclaim_disk()

    assert report.failures == ()
    assert report.total_bytes == 0


def test_render_marks_failed_steps(monkeypatch):
    def failed(cmd, **_):
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="daemon down")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", failed)
    rendered = reclaim.reclaim_disk().render()

    assert "FAILED" in rendered
    assert "daemon down" in rendered


def test_successful_reclaim_reports_no_failures(monkeypatch):
    def ok(cmd, **_):
        return CompletedProcess(args=cmd, returncode=0, stdout="Total reclaimed space: 1.0GB\n", stderr="")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", ok)
    report = reclaim.reclaim_disk()

    assert report.failures == ()
    assert "FAILED" not in report.render()


# --- Venue detection: a reclaim that never ran must not read as one that ran (#4585) ---
#
# MEASURED on the box: `teatree-admin` mounts /var/run/docker.sock and carries no
# `group_add`, so every prune answers "permission denied"; `teatree-worker` has
# the grant AND the control DB, so it is the route that works. The three doomed
# prunes are not attempted at all now, and — the part the acceptance bar turns
# on — a run that could not act never prints the `Total reclaimed:` line that
# reads as "nothing needed reclaiming".


def _venue(**kwargs) -> DockerVenue:
    defaults = {"reachable": False, "reason": "permission denied", "containerized": True, "service_role": "admin"}
    return DockerVenue(**(defaults | kwargs))


def test_an_unreachable_venue_attempts_no_prune_at_all(monkeypatch):
    fake = _FakePrune()
    monkeypatch.setattr(reclaim, "_run_prune", fake)
    monkeypatch.setattr(reclaim, "docker_venue", _venue)

    report = reclaim.reclaim_disk()

    assert fake.calls == [], "three prunes that cannot succeed must not be attempted"
    assert report.venue_blocked is True


def test_venue_blocked_render_omits_the_total_reclaimed_success_line(monkeypatch):
    """`Total reclaimed: 0B` beside a refusal reads as "nothing needed reclaiming"."""
    monkeypatch.setattr(reclaim, "docker_venue", _venue)

    rendered = reclaim.reclaim_disk().render()

    assert "Total reclaimed" not in rendered
    assert "did not run" in rendered
    assert "permission denied" in rendered


def test_venue_blocked_render_names_a_runnable_route(monkeypatch):
    """A refusal with no route to satisfaction is the defect; the route must be in the output."""
    monkeypatch.setattr(reclaim, "docker_venue", _venue)

    rendered = reclaim.reclaim_disk().render()

    assert "the admin container" in rendered
    assert "teatree-worker" in rendered
    assert "docker builder prune -af" in rendered


def test_the_remedy_commands_are_exactly_the_reclaim_set(monkeypatch):
    """The remedy is derived from the planned argv, so it can never drift from what would run."""
    monkeypatch.setattr(reclaim, "docker_venue", _venue)

    rendered = reclaim.reclaim_disk().render()

    for argv, _label in reclaim._SAFE_STEPS:
        assert " ".join(argv) in rendered
    assert "system prune" not in rendered


def test_a_worker_venue_refusal_still_names_the_host_route(monkeypatch):
    """Refused inside the ONE granted service: pointing back at it would be a loop."""
    monkeypatch.setattr(reclaim, "docker_venue", lambda: _venue(service_role="worker", reason="daemon down"))

    rendered = reclaim.reclaim_disk().render()

    assert "docker builder prune -af" in rendered
    assert "exec teatree-worker" not in rendered


def test_absent_docker_cli_is_venue_blocked_not_a_clean_zero(monkeypatch):
    """The last silent-success hole: nothing ran, yet the report read as a completed 0B reclaim."""
    monkeypatch.setattr(
        reclaim, "docker_venue", lambda: DockerVenue(reachable=False, reason="no docker CLI here", has_cli=False)
    )

    report = reclaim.reclaim_disk()

    assert report.venue_blocked is True
    assert "Total reclaimed" not in report.render()


def test_venue_blocked_summary_is_what_the_caller_puts_on_stderr(monkeypatch):
    monkeypatch.setattr(reclaim, "docker_venue", _venue)

    assert "permission denied" in reclaim.reclaim_disk().failure_summary()


def test_dry_run_reports_the_venue_but_never_claims_a_reclaim(monkeypatch):
    """A dry run removes nothing by construction, so it cannot be misread as a reclaim."""
    monkeypatch.setattr(reclaim, "docker_venue", _venue)

    report = reclaim.reclaim_disk(dry_run=True)

    assert report.venue_blocked is False
    rendered = report.render()
    assert "Dry run" in rendered
    assert "the admin container" in rendered


def test_a_reachable_venue_runs_the_three_prunes_unchanged(monkeypatch):
    """The venue gate must not narrow the ordinary path — the control for every test above."""
    fake = _FakePrune()
    monkeypatch.setattr(reclaim, "_run_prune", fake)
    monkeypatch.setattr(reclaim, "docker_venue", lambda: DockerVenue(reachable=True))

    report = reclaim.reclaim_disk()

    assert len(fake.calls) == 3
    assert report.venue_blocked is False
    assert "Total reclaimed" in report.render()
