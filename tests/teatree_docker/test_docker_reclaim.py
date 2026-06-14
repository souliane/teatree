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
from teatree.utils.run import TimeoutExpired


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


def test_run_prune_returns_zero_on_timeout(monkeypatch):
    def slow(cmd, **_):
        raise TimeoutExpired(cmd, 1)

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", slow)
    outcome = reclaim._run_prune(["docker", "builder", "prune", "-af"])
    assert outcome.bytes_reclaimed == 0


def test_run_prune_returns_zero_on_nonzero_exit(monkeypatch):
    def failed(cmd, **_):
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="daemon down")

    monkeypatch.setattr(reclaim, "run_allowed_to_fail", failed)
    outcome = reclaim._run_prune(["docker", "image", "prune", "-f"])
    assert outcome == reclaim.PruneOutcome(reclaimed="0B", bytes_reclaimed=0)


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
