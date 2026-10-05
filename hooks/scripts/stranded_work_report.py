"""The stranded-work report one session leaves for the next session in the same checkout.

Nothing a SessionEnd hook prints reaches a model, so the session-end check writes its report here and
the next ``SessionStart`` in that checkout delivers it and consumes it. One file per checkout, keyed by
its top directory (:func:`checkout_root`), so a session that moved into a subdirectory still reaches the
next one started at the top; replaced atomically by every end that finds work stranded, and cleared with
every claim of it (:func:`clear`, so no start puts it back) by one whose probes all answered and found
none. A report older than :data:`MAX_AGE_SECONDS` is dropped unread: by then the factory's own sweeps have
had the work, and an old list would send the next session after state that has moved. A finding dated by its
own :func:`found_line` ages out alone the same way, so one an end carries forward never outlives its age and
never takes a fresher finding beside it down with it. A claim whose start
died before delivering it is put back by the next start (:data:`IN_FLIGHT_AT_MOST_SECONDS`), unless an end
cleared the report since it was taken, and an end that looks while a start holds the report reads the claim
(:func:`peek`). Every end sweeps a bounded slice of the store (:data:`SWEEP_AT_MOST`) of what no start will
read again: any checkout's aged-out report, claim or clear stamp, and a staged write an interrupted end left.
Django-free and fail-open — a store that cannot be written or read leaves nothing and delivers nothing.
"""

import contextlib
import hashlib
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hooks.scripts.state_files import hook_state_dir

REPORT_DIRNAME = "stranded-work"
MAX_AGE_SECONDS = 2 * 24 * 60 * 60
#: A hook runs under a seconds-long timeout, so a claim or a staged write older than this is no live hook's,
#: whatever pid it names.
IN_FLIGHT_AT_MOST_SECONDS = 60
#: The most store entries one end looks at, so a hook keeps its time budget however full the store has grown.
#: They are drawn at random from the whole listing: a filesystem that lists its newest entries first (tmpfs)
#: would otherwise hide an old entry behind live ones forever, where a draw reaches every entry in time.
SWEEP_AT_MOST = 64
_CLAIMED_SUFFIX = ".claimed"
#: ``.<stem>.cleared``: its mtime is the instant an end last cleared that checkout's report (see :func:`clear`).
_CLEARED_SUFFIX = ".cleared"
_NS_PER_SECOND = 1_000_000_000
#: A staged write is named ``.staged.<when>.<random>.tmp``, so the sweep reads when it began from its name.
_STAGED_PREFIX = ".staged."
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
#: A finding is a ``  - `` line and the indented lines under it; one of them may date it (:func:`found_line`).
_FINDING_START = re.compile(r"^(?=  - )", re.MULTILINE)
_FOUND_PREFIX = "      found: "
_FOUND_LINE = re.compile(rf"^{_FOUND_PREFIX}(\S+)$", re.MULTILINE)


def checkout_root(cwd: str) -> Path:
    """The checkout holding *cwd* — the nearest directory at or above it with a ``.git`` — else *cwd* resolved.

    Git finds its top level the same way; the walk reads only the filesystem, so a hook pays no process.
    """
    start = Path(os.path.realpath(cwd))
    return next((directory for directory in (start, *start.parents) if (directory / ".git").exists()), start)


def _report_path(cwd: str) -> Path:
    digest = hashlib.sha256(str(checkout_root(cwd)).encode("utf-8")).hexdigest()[:32]
    return hook_state_dir() / REPORT_DIRNAME / f"{digest}.txt"


def _claims(home: Path) -> list[Path]:
    with contextlib.suppress(OSError):
        return list(home.parent.glob(f".{home.stem}.*{_CLAIMED_SUFFIX}"))
    return []


@dataclass(frozen=True, slots=True)
class StoredReport:
    """A report still in the store, and when the end whose findings it holds left it."""

    text: str
    left_at: float


def found_line(at: float) -> str:
    """The line under a finding that dates it to *at*, its first sighting; an end carrying it copies the line as is."""
    return f"{_FOUND_PREFIX}{datetime.fromtimestamp(int(at), tz=UTC).isoformat()}"


def found_at(finding: str) -> float | None:
    """When the first :func:`found_line` in *finding* says it was first seen, if it carries a readable one."""
    dated = _FOUND_LINE.search(finding)
    try:
        return datetime.fromisoformat(dated[1]).timestamp() if dated else None
    except ValueError:
        return None


def _unexpired(report: str, instant: float) -> str:
    """*report* less every finding its own date ages out; ``""`` when it held findings and none is left.

    A finding with no readable date ages with the report as a whole.
    """
    head, *findings = _FINDING_START.split(report)
    kept = [finding for finding in findings if instant - (found_at(finding) or instant) <= MAX_AGE_SECONDS]
    if findings and not kept:
        return ""
    return (head + "".join(kept)).rstrip("\n")


def _read(path: Path | None) -> StoredReport | None:
    if path is None:
        return None
    try:
        left_at = path.stat().st_mtime
        return StoredReport(path.read_text(encoding="utf-8"), left_at)
    except OSError:
        return None


def _newest_held(home: Path) -> Path | None:
    """The newest claim of *home* a start still holds and no end has cleared since it was taken."""
    held = [claimed for claimed in _claims(home) if not _cleared_since(claimed, home)]
    return max(held, key=_left_at, default=None)


def peek(cwd: str, *, now: float | None = None) -> StoredReport | None:
    """The report for *cwd* — stored, or held by a start that has not delivered it yet — left where it is.

    ``None`` if there is none, it is unreadable, or it aged out.
    """
    home = _report_path(cwd)
    instant = time.time() if now is None else now
    stored = _read(home) or _read(_newest_held(home))
    if stored is None or instant - stored.left_at > MAX_AGE_SECONDS:
        return None
    text = _unexpired(stored.text, instant)
    return StoredReport(text, stored.left_at) if text else None


def _sweep(store: Path) -> None:
    now = time.time()
    try:
        with os.scandir(store) as entries:
            names = [entry.name for entry in entries]
    except OSError:
        return
    for name in random.sample(names, min(len(names), SWEEP_AT_MOST)):
        path = store / name
        with contextlib.suppress(OSError):
            if _unread_again(path, now):
                path.unlink()


def _unread_again(entry: Path, now: float) -> bool:
    staged_at = entry.name.removeprefix(_STAGED_PREFIX).partition(".")[0]
    if entry.name.startswith(_STAGED_PREFIX) and staged_at.isdigit():
        return now - int(staged_at) > IN_FLIGHT_AT_MOST_SECONDS
    return now - entry.stat().st_mtime > MAX_AGE_SECONDS


def _cleared_stamp(home: Path) -> Path:
    return home.with_name(f".{home.stem}{_CLEARED_SUFFIX}")


def _stamp_cleared(home: Path, through_ns: int) -> None:
    stamp = _cleared_stamp(home)
    with contextlib.suppress(OSError):
        stamp.touch()
        os.utime(stamp, ns=(through_ns, through_ns))


def clear(cwd: str) -> None:
    """This end found nothing stranded: remove the report for *cwd* and every claim of it, so none is put back.

    The clear is stamped first through the longest a start can be in flight, so a report taken before it
    is gone and put back while this runs is taken out again (:meth:`ClaimedReport.put_back`). The claims are
    listed only once the report is gone, so none taken from it is missed. The stamp then comes back to now,
    which every claim taken before this clear predates. An end killed before that, or a clock stepped back,
    leaves the stamp up to :data:`IN_FLIGHT_AT_MOST_SECONDS` ahead, and a report put back inside it is dropped:
    the lease must cover an end stalled before it removes the report, and its cost is one advisory report
    lost, never a cleared one brought back.
    """
    home = _report_path(cwd)
    _sweep(home.parent)
    _stamp_cleared(home, time.time_ns() + IN_FLIGHT_AT_MOST_SECONDS * _NS_PER_SECOND)
    with contextlib.suppress(OSError):
        home.unlink(missing_ok=True)
    for claimed in _claims(home):
        with contextlib.suppress(OSError):
            claimed.unlink(missing_ok=True)
    _stamp_cleared(home, time.time_ns())


def leave(cwd: str, report: str) -> None:
    """Replace the report for *cwd*; an empty *report* removes it, leaving any claim to the start that holds it.

    The report is dated now; a finding it carries forward keeps its own age through its :func:`found_line`.
    """
    path = _report_path(cwd)
    _sweep(path.parent)
    if not report:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, staged = tempfile.mkstemp(prefix=f"{_STAGED_PREFIX}{int(time.time())}.", suffix=".tmp", dir=path.parent)
    except OSError:
        return
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(report)
        Path(staged).replace(path)
    except OSError:
        return
    finally:
        Path(staged).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ClaimedReport:
    """A report taken out of the store: consumed once delivered, put back when the delivery failed."""

    text: str = ""
    claimed: Path | None = None
    home: Path | None = None

    def consume(self) -> None:
        if self.claimed is not None:
            self.claimed.unlink(missing_ok=True)

    def put_back(self) -> None:
        """Return the report to the store, unless an end since the claim has left a newer one there or cleared it.

        A hard link never replaces what it would land on, as a rename does, so a newer report stands. A
        clear stamped at or after the claim, even one still running as the link lands, takes it out again —
        by the file the claim held before the link, since that clear may remove the claimed name meanwhile.
        The claimed name goes either way: a claim is never left behind for no session to find.
        """
        if self.claimed is None or self.home is None:
            return
        with contextlib.suppress(OSError):
            held = self.claimed.stat()
            os.link(self.claimed, self.home)
            landed = self.home.stat()
            if _cleared_since(self.claimed, self.home) and (landed.st_dev, landed.st_ino) == (held.st_dev, held.st_ino):
                self.home.unlink()
        with contextlib.suppress(OSError):
            self.claimed.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _taken(claimed: Path, home: Path) -> tuple[int, int] | None:
    """The pid and the instant (ns) a ``.<stem>.<pid>.<claimed at ns>.claimed`` name records, if it records both."""
    pid, _, claimed_at = claimed.name.removeprefix(f".{home.stem}.").removesuffix(_CLAIMED_SUFFIX).partition(".")
    return (int(pid), int(claimed_at)) if pid.isdigit() and int(pid) > 0 and claimed_at.isdigit() else None


def _cleared_since(claimed: Path, home: Path) -> bool:
    """Whether an end has cleared the report at or after the instant *claimed* was taken, or is clearing it now."""
    try:
        cleared_through = _cleared_stamp(home).stat().st_mtime_ns
    except OSError:
        return False
    taken = _taken(claimed, home)
    return taken is None or cleared_through >= taken[1]


def _abandoned(claimed: Path, home: Path, instant: float) -> bool:
    """Whether *claimed* is no live start's: it died, or held it too long."""
    taken = _taken(claimed, home)
    if taken is None:
        return True
    pid, claimed_at = taken
    return instant - claimed_at / _NS_PER_SECOND > IN_FLIGHT_AT_MOST_SECONDS or not _alive(pid)


def _left_at(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _take_back_abandoned(home: Path, instant: float) -> None:
    """Put back each claim no live start holds, newest report first, so an older one never replaces a newer."""
    abandoned = (claimed for claimed in _claims(home) if _abandoned(claimed, home, instant))
    for claimed in sorted(abandoned, key=_left_at, reverse=True):
        ClaimedReport(claimed=claimed, home=home).put_back()


def claim(cwd: str, *, now: float | None = None) -> ClaimedReport:
    """Take the report for *cwd* out of the store, rendered for the session about to read it.

    The rename makes a delivery single: a second session starting in the same checkout finds nothing.
    A report that cannot be read once claimed is put back for a later start, and one an earlier start
    claimed and never delivered (it died) is put back first.
    """
    if not cwd:
        return ClaimedReport()
    home = _report_path(cwd)
    instant_ns = time.time_ns() if now is None else int(now * _NS_PER_SECOND)
    instant = instant_ns / _NS_PER_SECOND
    _take_back_abandoned(home, instant)
    claimed = home.with_name(f".{home.stem}.{os.getpid()}.{instant_ns}{_CLAIMED_SUFFIX}")
    try:
        home.rename(claimed)
    except OSError:
        return ClaimedReport()
    taken = ClaimedReport(claimed=claimed, home=home)
    try:
        age = instant - claimed.stat().st_mtime
        report = _unexpired(claimed.read_text(encoding="utf-8"), instant) if age <= MAX_AGE_SECONDS else ""
    except OSError:
        taken.put_back()
        return ClaimedReport()
    if not report:
        taken.consume()
        return ClaimedReport()
    return ClaimedReport(_render(report, age), claimed, home)


def _render(report: str, age: float) -> str:
    minutes = max(int(age // _SECONDS_PER_MINUTE), 0)
    when = f"{minutes} min" if minutes < _MINUTES_PER_HOUR else f"{minutes // _MINUTES_PER_HOUR} h"
    return (
        f"Stranded work left by the session that ended in this checkout {when} ago — re-check each item "
        f"before acting, it may have moved since:\n{report}"
    )


__all__ = [
    "IN_FLIGHT_AT_MOST_SECONDS",
    "MAX_AGE_SECONDS",
    "REPORT_DIRNAME",
    "SWEEP_AT_MOST",
    "ClaimedReport",
    "StoredReport",
    "checkout_root",
    "claim",
    "clear",
    "found_at",
    "found_line",
    "leave",
    "peek",
]
