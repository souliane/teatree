# test-path: cross-cutting
# Drives hooks/scripts/stranded_work_report.py's store step by step (no src/teatree mirror): the
# interleavings of a put-back and a clear, the clear's stamp, and the sweep's reach.
"""The stranded-work store never brings back a cleared report, and its bounded sweep reaches every entry in time.

Each race is one deterministic interleaving: the other side's steps run inside a patched primitive of
this one, so the order is fixed, never left to a sleep.
"""

import itertools
import os
import random
import time
from pathlib import Path
from typing import Self

import pytest

from hooks.scripts import stranded_work_report

_PROJECT = "/work/project"
_REPORT = "UNSHIPPED WORK AT SESSION END\n  - [unpushed] /work/project (feature) — 2 commit(s) on no remote"
_NS = 1_000_000_000
_LEASE_NS = stranded_work_report.IN_FLIGHT_AT_MOST_SECONDS * _NS
_CLEAR_STAMP = stranded_work_report._cleared_stamp(stranded_work_report._report_path(_PROJECT)).name


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("T3_HOOK_STATE_DIR", str(directory))
    monkeypatch.setenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", str(directory))
    return directory


def _home() -> Path:
    """The report's path in the test's own store (it moves with ``T3_HOOK_STATE_DIR``)."""
    return stranded_work_report._report_path(_PROJECT)


def _stored(state: Path) -> list[str]:
    return sorted(path.name for path in (state / stranded_work_report.REPORT_DIRNAME).iterdir())


def test_a_report_whose_every_finding_aged_out_is_dropped_never_delivered_as_a_bare_header(state: Path) -> None:
    aged = time.time() - stranded_work_report.MAX_AGE_SECONDS - 60
    stranded_work_report.leave(_PROJECT, f"{_REPORT}\n{stranded_work_report.found_line(aged)}")

    assert stranded_work_report.peek(_PROJECT) is None
    assert stranded_work_report.claim(_PROJECT).text == ""
    assert not _home().exists()
    assert stranded_work_report._claims(_home()) == []


@pytest.mark.parametrize("putting_back", ["a-start-whose-write-failed", "a-later-start-taking-it-back"])
def test_a_put_back_whose_link_lands_as_an_end_clears_never_brings_the_report_back(
    state: Path, monkeypatch: pytest.MonkeyPatch, putting_back: str
) -> None:
    # The end has stamped its lease and removed the report; the put-back's link lands, and only then does the
    # end list the claims, remove this one and bring its stamp back to now, before the put-back looks again.
    stranded_work_report.leave(_PROJECT, _REPORT)
    taken = stranded_work_report.claim(_PROJECT)
    stranded_work_report._stamp_cleared(_home(), time.time_ns() + _LEASE_NS)
    link = os.link

    def linked_as_the_end_finishes_its_clear(source: Path, target: Path) -> None:
        link(source, target)
        for claimed in stranded_work_report._claims(_home()):
            claimed.unlink()
        stranded_work_report._stamp_cleared(_home(), time.time_ns())

    monkeypatch.setattr(stranded_work_report.os, "link", linked_as_the_end_finishes_its_clear)
    if putting_back == "a-start-whose-write-failed":
        taken.put_back()
        delivered = ""
    else:
        later = time.time() + stranded_work_report.IN_FLIGHT_AT_MOST_SECONDS + 1
        delivered = stranded_work_report.claim(_PROJECT, now=later).text
    monkeypatch.setattr(stranded_work_report.os, "link", link)

    assert delivered == ""
    assert stranded_work_report.claim(_PROJECT).text == ""
    assert _stored(state) == [_CLEAR_STAMP]


@pytest.mark.usefixtures("state")
def test_a_report_left_after_a_clear_and_put_back_in_the_same_second_reaches_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The clear's stamp comes back to the instant it finished, and a claim records its own instant to the
    # nanosecond, so a report left, taken and put back within that same second still stands.
    ticks = itertools.count(int(time.time()) * _NS + _NS // 2, 1_000)
    monkeypatch.setattr(stranded_work_report.time, "time_ns", lambda: next(ticks))
    stranded_work_report.leave(_PROJECT, "  - [unpushed] an older report")
    stranded_work_report.clear(_PROJECT)
    stranded_work_report.leave(_PROJECT, _REPORT)

    stranded_work_report.claim(_PROJECT).put_back()

    assert _REPORT in stranded_work_report.claim(_PROJECT).text


def test_a_report_put_back_inside_the_lease_a_killed_clear_left_is_dropped_and_none_after_it(state: Path) -> None:
    # The accepted cost of the lease: an end killed after stamping it (or a clock stepped back) drops a report
    # put back inside it — one advisory report lost, never a cleared one brought back — and nothing after it.
    stranded_work_report.leave(_PROJECT, "  - [unpushed] the report the killed end removed")
    stranded_work_report._stamp_cleared(_home(), time.time_ns() + _LEASE_NS)
    _home().unlink()
    stranded_work_report.leave(_PROJECT, "  - [unpushed] left inside the lease")
    stranded_work_report.claim(_PROJECT).put_back()

    assert stranded_work_report.claim(_PROJECT).text == ""
    assert _stored(state) == [_CLEAR_STAMP]

    past_the_lease = time.time() + stranded_work_report.IN_FLIGHT_AT_MOST_SECONDS + 1
    stranded_work_report.leave(_PROJECT, _REPORT)
    stranded_work_report.claim(_PROJECT, now=past_the_lease).put_back()
    assert _REPORT in stranded_work_report.claim(_PROJECT, now=past_the_lease + 1).text


class _Listing:
    """What ``os.scandir`` hands back — iterable and a context manager — over a fixed order."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def test_every_stale_entry_is_swept_in_time_when_the_store_lists_its_newest_entries_first(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tmpfs lists the newest entry first: a slice in listing order would never reach an entry behind
    # more live ones than the slice holds.
    store = state / stranded_work_report.REPORT_DIRNAME
    store.mkdir()
    now = time.time()
    aged = now - stranded_work_report.MAX_AGE_SECONDS - 60
    stale = [store / f"{index:032x}.txt" for index in range(8)]
    live = [store / f"{index:032x}.txt" for index in range(8, 8 + stranded_work_report.SWEEP_AT_MOST + 40)]
    for offset, path in enumerate([*stale, *live]):
        path.write_text("x", encoding="utf-8")
        when = aged if path in stale else now - len(live) + offset
        os.utime(path, (when, when))
    scandir = os.scandir

    def newest_first(path: str | os.PathLike[str] | int = ".") -> _Listing:
        with scandir(path) as listed:
            entries = list(listed)
        if not isinstance(path, int) and Path(path) == store:
            entries.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
        return _Listing(entries)

    monkeypatch.setattr(stranded_work_report.os, "scandir", newest_first)
    drawn_before = random.getstate()
    random.seed(20261005)
    try:
        for _ in range(20):
            stranded_work_report.leave(_PROJECT, "")
    finally:
        random.setstate(drawn_before)

    assert not any(path.exists() for path in stale)
    assert all(path.exists() for path in live)
