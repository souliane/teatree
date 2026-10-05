# test-path: cross-cutting
# Runs hooks/scripts/stranded_work_start.py as the SessionStart hook process it is, over the report
# hooks/scripts/stranded_work_report.py keeps (no src/teatree mirror).
"""The work a session left stranded reaches the next session in the same checkout, once, at its start.

Nothing a SessionEnd hook prints reaches a model, so the session-end check leaves its report in the hook
state dir keyed by the checkout, and the next ``SessionStart`` there delivers it as model-visible context
and consumes it. A report older than :data:`stranded_work_report.MAX_AGE_SECONDS` is dropped unread: the
work has had the factory's own sweeps to move it since.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from hooks.scripts import stranded_work_report
from tests._cold_hook_guard import COLD_SCRIPT_DRIVER, FAILED_WRITE_DRIVER, FORBIDDEN
from tests._unreadable_file import skip_if_root

_HOOKS = Path(__file__).resolve().parents[2] / "hooks"
_SCRIPT = _HOOKS / "scripts" / "stranded_work_start.py"
_PROJECT = "/work/project"
_REPORT = "UNSHIPPED WORK AT SESSION END (1)\n  - [unpushed] /work/project (feature) — 2 commit(s) on no remote"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    monkeypatch.setenv("T3_HOOK_STATE_DIR", str(directory))
    monkeypatch.setenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", str(directory))
    return directory


_LANE_KEYS = frozenset({"CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT"})


def _start(state: Path, cwd: str = _PROJECT, *, failed_write: str = "", sdk: bool = False) -> str:
    """The model-visible SessionStart context: ONE nested object, or nothing."""
    driver = [FAILED_WRITE_DRIVER, str(_HOOKS.parent), failed_write] if failed_write else [COLD_SCRIPT_DRIVER]
    env = {key: value for key, value in os.environ.items() if key not in _LANE_KEYS}
    if sdk:
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
    result = subprocess.run(
        [sys.executable, "-c", *driver, str(_SCRIPT)],
        input=json.dumps({"session_id": "next", "source": "startup", "cwd": cwd, "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**env, "T3_HOOK_STATE_DIR": str(state), "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state)},
        cwd=state,
    )
    assert result.returncode == 0, result.stderr
    assert FORBIDDEN not in result.stderr
    if not result.stdout:
        return ""
    document = json.loads(result.stdout)
    context = document["hookSpecificOutput"]["additionalContext"]
    assert document == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    return context


def test_the_next_session_in_the_checkout_gets_the_report_once(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)

    context = _start(state)

    assert _REPORT in context
    assert "re-check each item" in context
    assert _start(state) == ""


def test_a_session_in_another_checkout_gets_nothing(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)

    assert _start(state, "/work/elsewhere") == ""
    assert _REPORT in _start(state)


def test_two_sessions_starting_at_once_get_the_report_once(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)

    first = stranded_work_report.claim(_PROJECT)
    second = stranded_work_report.claim(_PROJECT)

    assert _REPORT in first.text
    assert second.text == ""


def test_a_session_that_ended_in_a_subdirectory_reaches_the_next_at_the_checkout_top(
    state: Path, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "src" / "pkg").mkdir(parents=True)
    stranded_work_report.leave(str(checkout / "src" / "pkg"), _REPORT)

    assert _REPORT in _start(state, str(checkout))


def test_an_sdk_session_start_leaves_the_report_for_the_interactive_one(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)

    assert _start(state, sdk=True) == ""

    assert _REPORT in _start(state)


def test_an_aged_out_report_is_dropped_unread(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)
    stale = time.time() - stranded_work_report.MAX_AGE_SECONDS - 60
    path = next((state / stranded_work_report.REPORT_DIRNAME).iterdir())
    os.utime(path, (stale, stale))

    assert _start(state) == ""
    assert not any((state / stranded_work_report.REPORT_DIRNAME).iterdir())


@pytest.mark.parametrize("failure", ["broken-pipe", "unread-pipe"])
def test_a_failed_write_keeps_the_report_for_the_next_start(state: Path, failure: str) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)

    assert _start(state, failed_write=failure) == ""

    assert _REPORT in _start(state)


def _stored(state: Path) -> list[str]:
    return sorted(path.name for path in (state / stranded_work_report.REPORT_DIRNAME).iterdir())


#: What a clear leaves in the store: the stamp a put-back of a report taken before it checks.
_CLEAR_STAMP = stranded_work_report._cleared_stamp(stranded_work_report._report_path(_PROJECT)).name


@skip_if_root
def test_a_report_the_start_cannot_read_stays_in_the_store_for_a_later_start(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)
    [name] = _stored(state)
    report = state / stranded_work_report.REPORT_DIRNAME / name
    report.chmod(0)

    assert _start(state) == ""

    assert _stored(state) == [name]
    report.chmod(0o600)
    assert _REPORT in _start(state)


def test_a_report_put_back_never_replaces_a_newer_one(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, "  - [unpushed] an older report")
    taken = stranded_work_report.claim(_PROJECT)
    stranded_work_report.leave(_PROJECT, _REPORT)

    taken.put_back()

    assert len(_stored(state)) == 1
    context = _start(state)
    assert _REPORT in context
    assert "an older report" not in context


def _claimed_by_a_start_that_died(state: Path) -> None:
    """A start that took the report out of the store and died before writing it or putting it back."""
    claim = f"from hooks.scripts import stranded_work_report; stranded_work_report.claim({_PROJECT!r})"
    subprocess.run(
        [sys.executable, "-c", claim],
        check=True,
        timeout=60,
        env={**os.environ, "T3_HOOK_STATE_DIR": str(state), "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(state)},
        cwd=_HOOKS.parent,
    )


def test_a_report_a_dead_start_took_reaches_the_next_start_once(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)
    _claimed_by_a_start_that_died(state)

    assert _REPORT in _start(state)
    assert _start(state) == ""
    assert _stored(state) == []


def test_a_live_starts_claim_is_not_taken(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)
    taken = stranded_work_report.claim(_PROJECT)

    assert _start(state) == ""

    taken.put_back()
    assert _REPORT in _start(state)


def test_a_claim_held_past_any_start_is_taken_back_whatever_its_pid_now_names(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)
    stranded_work_report.claim(_PROJECT)

    later = time.time() + stranded_work_report.IN_FLIGHT_AT_MOST_SECONDS + 1
    assert _REPORT in stranded_work_report.claim(_PROJECT, now=later).text


def test_a_dead_starts_claim_never_replaces_a_newer_report(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, "  - [unpushed] an older report")
    _claimed_by_a_start_that_died(state)
    stranded_work_report.leave(_PROJECT, "  - [unpushed] a middle report")
    _claimed_by_a_start_that_died(state)
    stranded_work_report.leave(_PROJECT, _REPORT)

    context = _start(state)

    assert _REPORT in context
    assert "an older report" not in context
    assert "a middle report" not in context
    assert _start(state) == ""


def test_the_newest_of_two_abandoned_claims_is_the_one_put_back(state: Path) -> None:
    now = time.time()
    stranded_work_report.leave(_PROJECT, "  - [unpushed] an older report")
    os.utime(next((state / stranded_work_report.REPORT_DIRNAME).iterdir()), (now - 600, now - 600))
    stranded_work_report.claim(_PROJECT, now=now)
    stranded_work_report.leave(_PROJECT, _REPORT)
    stranded_work_report.claim(_PROJECT, now=now + 1)

    taken = stranded_work_report.claim(_PROJECT, now=now + stranded_work_report.IN_FLIGHT_AT_MOST_SECONDS + 2)

    assert _REPORT in taken.text
    assert "an older report" not in taken.text
    assert taken.claimed is not None
    assert _stored(state) == [taken.claimed.name]


@pytest.mark.parametrize("claimant", ["live", "dead"])
def test_an_end_that_found_nothing_stranded_retires_a_claimed_report(state: Path, claimant: str) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)
    taken = stranded_work_report.claim(_PROJECT) if claimant == "live" else None
    if taken is None:
        _claimed_by_a_start_that_died(state)

    stranded_work_report.clear(_PROJECT)

    if taken is not None:
        taken.put_back()
    assert _start(state) == ""
    assert _stored(state) == [_CLEAR_STAMP]


def test_a_report_taken_after_a_clearing_end_listed_the_claims_is_never_put_back(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The start takes the report right after the end listed the claims, and puts it back once the end is done.
    stranded_work_report.leave(_PROJECT, _REPORT)
    listing = stranded_work_report._claims
    taken: list[stranded_work_report.ClaimedReport] = []

    def listed_then_taken(home: Path) -> list[Path]:
        claims = listing(home)
        monkeypatch.setattr(stranded_work_report, "_claims", listing)
        taken.append(stranded_work_report.claim(_PROJECT))
        return claims

    monkeypatch.setattr(stranded_work_report, "_claims", listed_then_taken)
    stranded_work_report.clear(_PROJECT)
    assert _stored(state) == [_CLEAR_STAMP], "the clear left a claim of the report it removed"
    taken[0].put_back()

    assert _start(state) == ""


@pytest.mark.parametrize("taken_as_the_clear_began", [False, True], ids=["taken-before", "taken-once-stamped"])
def test_a_report_put_back_while_an_end_clears_it_stays_cleared(
    state: Path, monkeypatch: pytest.MonkeyPatch, *, taken_as_the_clear_began: bool
) -> None:
    # The put-back lands after the end removed the report and before it lists the claims.
    stranded_work_report.leave(_PROJECT, _REPORT)
    taken = [] if taken_as_the_clear_began else [stranded_work_report.claim(_PROJECT)]
    stamp, listing = stranded_work_report._stamp_cleared, stranded_work_report._claims

    def put_back_then_listed(home: Path) -> list[Path]:
        monkeypatch.setattr(stranded_work_report, "_claims", listing)
        taken[0].put_back()
        return listing(home)

    def stamped_then_taken(home: Path, through_ns: int) -> None:
        stamp(home, through_ns)
        monkeypatch.setattr(stranded_work_report, "_stamp_cleared", stamp)
        taken.append(stranded_work_report.claim(_PROJECT))
        monkeypatch.setattr(stranded_work_report, "_claims", put_back_then_listed)

    if taken_as_the_clear_began:
        monkeypatch.setattr(stranded_work_report, "_stamp_cleared", stamped_then_taken)
    else:
        monkeypatch.setattr(stranded_work_report, "_claims", put_back_then_listed)
    stranded_work_report.clear(_PROJECT)

    assert _start(state) == ""
    assert _stored(state) == [_CLEAR_STAMP]


def test_an_end_that_looks_while_another_clears_does_not_carry_the_cleared_report(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)
    stranded_work_report.claim(_PROJECT)
    listing = stranded_work_report._claims
    seen: list[stranded_work_report.StoredReport | None] = []

    def looked_then_listed(home: Path) -> list[Path]:
        monkeypatch.setattr(stranded_work_report, "_claims", listing)
        seen.append(stranded_work_report.peek(_PROJECT))
        return listing(home)

    monkeypatch.setattr(stranded_work_report, "_claims", looked_then_listed)
    stranded_work_report.clear(_PROJECT)

    assert seen == [None]
    assert _stored(state) == [_CLEAR_STAMP]


def test_a_newer_report_left_as_a_put_back_lands_during_a_clear_stands(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stranded_work_report.leave(_PROJECT, "  - [unpushed] an older report")
    taken = stranded_work_report.claim(_PROJECT)
    clearing_until = time.time_ns() + stranded_work_report.IN_FLIGHT_AT_MOST_SECONDS * 1_000_000_000
    stranded_work_report._stamp_cleared(stranded_work_report._report_path(_PROJECT), clearing_until)
    link = os.link

    def linked_then_replaced(source: Path, target: Path) -> None:
        link(source, target)
        stranded_work_report.leave(_PROJECT, _REPORT)

    monkeypatch.setattr(stranded_work_report.os, "link", linked_then_replaced)
    taken.put_back()
    monkeypatch.setattr(stranded_work_report.os, "link", link)

    context = _start(state)
    assert _REPORT in context
    assert "an older report" not in context


def test_an_end_sweeps_a_bounded_slice_of_an_overgrown_store_and_later_ends_the_rest(state: Path) -> None:
    store = state / stranded_work_report.REPORT_DIRNAME
    store.mkdir()
    aged = time.time() - stranded_work_report.MAX_AGE_SECONDS - 60
    for index in range(stranded_work_report.SWEEP_AT_MOST + 5):
        (store / f"{index:032x}.txt").write_text("x", encoding="utf-8")
        os.utime(store / f"{index:032x}.txt", (aged, aged))

    stranded_work_report.leave(_PROJECT, "")
    assert len(_stored(state)) == 5

    stranded_work_report.leave(_PROJECT, "")
    assert _stored(state) == []


@pytest.mark.parametrize(
    "end",
    [
        lambda: stranded_work_report.leave(_PROJECT, _REPORT),
        lambda: stranded_work_report.leave(_PROJECT, ""),
        lambda: stranded_work_report.clear(_PROJECT),
    ],
    ids=["found-work", "could-not-look", "found-nothing"],
)
def test_every_end_sweeps_what_no_start_will_read_again(state: Path, end: Callable[[], None]) -> None:
    store = state / stranded_work_report.REPORT_DIRNAME
    store.mkdir()
    now = int(time.time())
    aged = now - stranded_work_report.MAX_AGE_SECONDS - 60
    unfinished = now - stranded_work_report.IN_FLIGHT_AT_MOST_SECONDS - 1
    swept = [f"{'0' * 32}.txt", f".{'1' * 32}.4194300.{aged}.claimed", f".{'4' * 32}.cleared"]
    kept = [f"{'2' * 32}.txt", f".{'3' * 32}.4194300.{now}.claimed", f".{'5' * 32}.cleared", f".staged.{now}.ef.tmp"]
    for name in [*swept, *kept, f".staged.{unfinished}.ab.tmp"]:
        (store / name).write_text("x", encoding="utf-8")
    for name in swept:
        os.utime(store / name, (aged, aged))
    swept.append(f".staged.{unfinished}.ab.tmp")

    end()

    assert not any((store / name).exists() for name in swept)
    assert all((store / name).exists() for name in kept)


def test_an_end_leaves_no_staged_write_behind(state: Path) -> None:
    stranded_work_report.leave(_PROJECT, _REPORT)

    assert [name for name in _stored(state) if name.endswith(".tmp")] == []


def test_it_is_registered_on_session_start_in_its_own_bounded_process() -> None:
    groups = json.loads((_HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    commands = {hook["command"]: hook["timeout"] for group in groups for hook in group["hooks"]}

    script = "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/stranded_work_start.py"
    assert commands[f"${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/run-hook.sh {script}"] == 5
