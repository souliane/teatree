"""Synthetic ``/proc`` builder for the leaderless-process-group reader (#4580).

The box this reader runs on normally has ZERO leaderless groups, so a test that
asserts "nothing found" against the live table passes whether or not the reader
works. Every test here plants its own group and asserts the reader goes RED on
it — the control that makes the silent cases meaningful.

``stat``'s comm field may itself contain spaces and parens, so the fields are
written positionally exactly as the kernel emits them and the reader is left to
find the closing paren from the right.
"""

from dataclasses import dataclass, field
from pathlib import Path

#: Positional count after ``(comm)`` that reaches ``starttime`` (field 22).
_STAT_TAIL_FIELDS = 20
_STATE = 0
_PPID = 1
_PGRP = 2
_UTIME = 11
_STIME = 12
_STARTTIME = 19
_TAB = "\t"


def plant_uptime(proc_root: Path, seconds: float) -> None:
    """Write the table's own ``uptime``, the clock every age is measured against."""
    proc_root.mkdir(parents=True, exist_ok=True)
    (proc_root / "uptime").write_text(f"{seconds} {seconds}\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class PlantedProcess:
    """One synthetic pid, written as the three files the reader reads."""

    pid: int
    pgid: int
    state: str = "S"
    comm: str = "sh"
    ppid: int = 1
    cpu_ticks: int = 0
    start_ticks: int = 0
    cmdline: str = "sh -c :"
    nspids: tuple[int, ...] = field(default_factory=tuple)

    def write(self, proc_root: Path) -> Path:
        pid_dir = proc_root / str(self.pid)
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "stat").write_text(self._stat_line(), encoding="utf-8")
        (pid_dir / "cmdline").write_bytes(self.cmdline.replace(" ", "\x00").encode("utf-8") + b"\x00")
        if self.nspids:
            rendered = _TAB.join(str(value) for value in self.nspids)
            (pid_dir / "status").write_text(f"Name:{_TAB}{self.comm}\nNSpid:{_TAB}{rendered}\n", encoding="utf-8")
        return pid_dir

    def _stat_line(self) -> str:
        fields = ["0"] * _STAT_TAIL_FIELDS
        fields[_STATE] = self.state
        fields[_PPID] = str(self.ppid)
        fields[_PGRP] = str(self.pgid)
        fields[_UTIME] = str(self.cpu_ticks)
        fields[_STIME] = "0"
        fields[_STARTTIME] = str(self.start_ticks)
        return f"{self.pid} ({self.comm}) {' '.join(fields)}\n"
