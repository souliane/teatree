"""Leaderless process groups — the load nobody owns and nothing reaps (#4580).

A process group whose leader has exited leaves its members reparented to PID 1 with no
parent, no owner and no reaper. One such group — 37 shells from a fuzz run whose mutation
corrupted a loop's own exit keyword — ran for 9 days 10 hours on this box.

Its whole signature is that it looks fine on every per-process metric: niced, ~0.0%
instantaneous CPU, mostly in ``S`` state. The cost is LOAD AVERAGE, which counts runnable
threads rather than CPU, and which
:func:`~teatree.core.admission_governor.box_load_headroom` throttles admission on. Measured,
15 near-idle orphans removed ~58% of the factory's admitted capacity while every surface
read healthy.

The detection rule is the one that fired on all 37 and, as far as could be established, on
nothing else: a group whose LEADER DOES NOT EXIST, whose oldest member is past an age
threshold, and which is still BURNING. "Burning" is deliberately two signals. A runnable
member is the literal rule, but the incident's own ``ps`` snapshot caught the members
mostly sleeping, so a single instantaneous sample is a coin flip; :data:`_MIN_BURN_RATE`
is the corroborator that does not depend on when you looked — the incident averaged ~0.57
of a core across its lifetime, while a parked orphan (a detached ``sleep``, an idle nohup)
averages ~0.000. Cumulative CPU would not do: a group that burned hard and then went idle
is not the thing being looked for.

Two tables, and only one of them may be ACTED on. This venue's own ``/proc`` numbers pids
this process can signal; ``/host-proc`` numbers them in the host's namespace, where the
same integer names a different process here. So a venue group is reported ``signalable``
and a host group is not, and the two scans are made a partition rather than a double count
by ``NSpid`` — a host pid reporting more than one namespace entry is a container's, which
the venue scan already owns.
"""

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from teatree.core.cleanup.process_table import host_proc_root, venue_proc_root

#: Sustained fraction of one core, summed across the group, below which a leaderless group
#: is parked rather than spinning. The incident measured ~0.57; an idle orphan ~0.000.
_MIN_BURN_RATE = 0.05

#: Kernel states that count toward the load average the admission governor reads.
_RUNNABLE_STATES = frozenset({"R", "D"})

_CLOCK_TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")
_STATE = 0
_PPID = 1
_UTIME = 11
_STIME = 12
_STARTTIME = 19
_MIN_STAT_FIELDS = 20
#: Floors the burn-rate divisor so a group born this instant cannot divide by zero.
_MIN_RATE_WINDOW_SECONDS = 1.0
_PGRP = 2


@dataclass(frozen=True, slots=True)
class GroupMember:
    """One process of a leaderless group, as its own table presents it."""

    pid: int
    comm: str
    state: str
    argv: tuple[str, ...]

    @property
    def program(self) -> str:
        """``argv[0]``'s basename — the executable, with the directory it happened to run from stripped.

        The directory is NOT part of the identity, and treating it as such is how a
        never-reap list stops working: on a box whose every checkout sits under a path
        containing the product name, matching that name anywhere in the command line
        protects every process on the machine.
        """
        return Path(self.argv[0]).name if self.argv else self.comm

    @property
    def cmdline(self) -> str:
        return " ".join(self.argv)


@dataclass(frozen=True, slots=True)
class OrphanGroup:
    """A process group with no living leader, and the evidence that it is costing capacity."""

    pgid: int
    members: tuple[GroupMember, ...]
    age_seconds: float
    cpu_seconds: float
    signalable: bool
    source: str

    @property
    def burn_rate(self) -> float:
        """Cores burned per wall-clock second, summed across the group."""
        return self.cpu_seconds / max(self.age_seconds, _MIN_RATE_WINDOW_SECONDS)

    @property
    def runnable(self) -> bool:
        return any(member.state in _RUNNABLE_STATES for member in self.members)

    def remedy(self) -> str:
        """The command that reclaims this group, in the venue that can actually signal it."""
        if self.signalable:
            return f"t3 tool reap-orphan-groups --pgid {self.pgid} --apply"
        return (
            f"kill -TERM -{self.pgid} ON THE HOST — {self.source} numbers pids in the host's "
            "namespace, so signalling them from here would reach a different process"
        )

    def report(self) -> str:
        oldest = self.members[0]
        return (
            f"pgid {self.pgid}: {len(self.members)} process(es) with no group leader, "
            f"{self.age_seconds / 3600:.1f}h old, {self.cpu_seconds / 3600:.1f} CPU-hours burned "
            f"({self.burn_rate:.2f} cores sustained, runnable={self.runnable}) — "
            f"e.g. pid {oldest.pid} ({oldest.comm}) {oldest.cmdline!r}"
        )


@dataclass(frozen=True, slots=True)
class OrphanSurvey:
    """Every leaderless group this box can be asked about, and what could not be asked."""

    groups: tuple[OrphanGroup, ...]
    gaps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Stat:
    """The ``/proc/<pid>/stat`` fields this reader needs."""

    pid: int
    comm: str
    state: str
    ppid: int
    pgid: int
    cpu_ticks: int
    start_ticks: int


def min_age_seconds_setting() -> float:
    """The operator's report threshold; the shipped default when the setting is unreadable."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    try:
        return float(get_effective_settings().orphan_group_min_age_hours) * 3600
    except Exception:  # noqa: BLE001 — a diagnostic must never crash on its own knob
        return _DEFAULT_MIN_AGE_HOURS * 3600


_DEFAULT_MIN_AGE_HOURS = 6


def _read_uptime(root: Path) -> float | None:
    """The TABLE's own clock — never this process's, which may be a different namespace."""
    try:
        return float((root / "uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_stat(pid_dir: Path) -> _Stat | None:
    """Parse one ``stat``, or ``None`` when the pid vanished or the line is malformed."""
    try:
        raw = (pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        # comm is the ONLY field that may contain spaces and parens, so it is bounded by
        # the LAST ')' rather than tokenised.
        open_paren = raw.index("(")
        close_paren = raw.rindex(")")
        fields = raw[close_paren + 1 :].split()
        if len(fields) < _MIN_STAT_FIELDS:
            return None
        return _Stat(
            pid=int(raw[:open_paren].strip()),
            comm=raw[open_paren + 1 : close_paren],
            state=fields[_STATE],
            ppid=int(fields[_PPID]),
            pgid=int(fields[_PGRP]),
            cpu_ticks=int(fields[_UTIME]) + int(fields[_STIME]),
            start_ticks=int(fields[_STARTTIME]),
        )
    except ValueError:
        return None


def _read_argv(pid_dir: Path) -> tuple[str, ...]:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(part for part in raw.decode("utf-8", errors="replace").split("\x00") if part)


def _is_nested_namespace(pid_dir: Path) -> bool:
    """True iff this pid reports a namespace BELOW the table's own — i.e. it is a container's.

    An unreadable or absent ``NSpid`` answers False: over-reporting an advisory finding is
    recoverable, dropping one silently is not.
    """
    try:
        status = (pid_dir / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("NSpid:"):
            return len(line.split()) > _MIN_NSPID_FIELDS
    return False


#: ``NSpid:`` plus one entry — a pid living only in the table's own namespace.
_MIN_NSPID_FIELDS = 2


def scan_orphan_groups(
    root: Path,
    *,
    signalable: bool,
    min_age_seconds: float,
    exclude_nested_namespaces: bool = False,
) -> list[OrphanGroup]:
    """Every leaderless, aged, still-burning group in the table at *root*."""
    uptime = _read_uptime(root)
    if uptime is None:
        return []
    stats = _collect(root, exclude_nested_namespaces=exclude_nested_namespaces)
    live_pids = {stat.pid for stat in stats}
    grouped: dict[int, list[_Stat]] = {}
    for stat in stats:
        if stat.pgid > 0 and stat.pgid not in live_pids:
            grouped.setdefault(stat.pgid, []).append(stat)
    found = [
        _build_group(pgid, members, uptime=uptime, signalable=signalable, source=str(root))
        for pgid, members in sorted(grouped.items())
    ]
    return [group for group in found if group.age_seconds >= min_age_seconds and _is_burning(group)]


def _collect(root: Path, *, exclude_nested_namespaces: bool) -> list[_Stat]:
    try:
        pid_dirs = [entry for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return []
    collected: list[_Stat] = []
    for pid_dir in pid_dirs:
        if exclude_nested_namespaces and _is_nested_namespace(pid_dir):
            continue
        stat = _read_stat(pid_dir)
        if stat is not None:
            collected.append(stat)
    return collected


def _is_burning(group: OrphanGroup) -> bool:
    return group.runnable or group.burn_rate >= _MIN_BURN_RATE


def _build_group(
    pgid: int,
    stats: list[_Stat],
    *,
    uptime: float,
    signalable: bool,
    source: str,
) -> OrphanGroup:
    ordered = sorted(stats, key=lambda stat: stat.start_ticks)
    oldest_started = ordered[0].start_ticks / _CLOCK_TICKS_PER_SECOND
    return OrphanGroup(
        pgid=pgid,
        members=tuple(
            GroupMember(
                pid=stat.pid,
                comm=stat.comm,
                state=stat.state,
                argv=_read_argv(Path(source) / str(stat.pid)),
            )
            for stat in ordered
        ),
        age_seconds=max(0.0, uptime - oldest_started),
        cpu_seconds=sum(stat.cpu_ticks for stat in ordered) / _CLOCK_TICKS_PER_SECOND,
        signalable=signalable,
        source=source,
    )


def venue_ancestry_pgids() -> set[int]:
    """Every process group in THIS process's parent chain — the groups a reaper may not kill.

    The reaper runs inside the session's own process tree (the harness, its shell, this
    python), so signalling any group along that chain terminates the thing doing the
    reaping. Mirrors the ancestry protection the RAM reaper has carried since #128.
    """
    protected = {0, 1}
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        with contextlib.suppress(OSError):
            protected.add(os.getpgid(pid))
        stat = _read_stat(Path("/proc") / str(pid))
        if stat is None or stat.ppid <= 0:
            break
        pid = stat.ppid
    return protected


def survey_orphan_groups(*, min_age_seconds: float) -> OrphanSurvey:
    """Both tables this box can be asked about, partitioned so nothing is counted twice."""
    groups: list[OrphanGroup] = []
    gaps: list[str] = []
    venue = venue_proc_root()
    if venue is None:
        gaps.append("this venue's own /proc is unreadable, so no group here can be reported or reaped")
    else:
        groups.extend(scan_orphan_groups(venue, signalable=True, min_age_seconds=min_age_seconds))
    host, refusal = host_proc_root()
    if host is None:
        gaps.append(refusal)
    elif host != venue:
        groups.extend(
            scan_orphan_groups(
                host,
                signalable=False,
                min_age_seconds=min_age_seconds,
                exclude_nested_namespaces=True,
            )
        )
    return OrphanSurvey(groups=tuple(groups), gaps=tuple(gaps))


__all__ = [
    "GroupMember",
    "OrphanGroup",
    "OrphanSurvey",
    "min_age_seconds_setting",
    "scan_orphan_groups",
    "survey_orphan_groups",
    "venue_ancestry_pgids",
]
