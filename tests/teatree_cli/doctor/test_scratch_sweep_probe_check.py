"""``_check_scratch_sweep_probe`` — the armed-but-inoperative scratch lane (#4165).

``scratch_retention_days > 0`` with a probe that cannot see the process table is
the worst state to be in silently: the lane reads as configured, refuses on every
tick, and reclaims nothing. Surfacing-only, like its tmpfs siblings.
"""

from pathlib import Path

from teatree.cli.doctor.checks_resources import _check_scratch_sweep_probe
from tests._procfs import answering_pid


class TestScratchSweepProbeCheck:
    def test_warns_when_armed_but_the_probe_cannot_see_the_table(self, tmp_path: Path, capsys) -> None:
        assert _check_scratch_sweep_probe(retention_days=3, proc_root=tmp_path / "not-a-process-table") is True

        out = capsys.readouterr().out
        assert "WARN" in out
        assert "scratch_retention_days=3" in out
        assert "scratch_retention_days 0" in out

    def test_silent_when_the_lane_is_disabled(self, tmp_path: Path, capsys) -> None:
        assert _check_scratch_sweep_probe(retention_days=0, proc_root=tmp_path / "not-a-process-table") is True

        assert capsys.readouterr().out == ""

    def test_silent_when_the_probe_is_sighted(self, tmp_path: Path, capsys) -> None:
        proc = tmp_path / "proc"
        proc.mkdir()
        answering_pid(proc, tmp_path / "held")

        assert _check_scratch_sweep_probe(retention_days=3, proc_root=proc) is True

        assert capsys.readouterr().out == ""
