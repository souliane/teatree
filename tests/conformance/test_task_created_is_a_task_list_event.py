# test-path: cross-cutting — a whole-tree harness-contract invariant; no src/teatree/ mirror.
"""``TaskCreated`` is the task-LIST tools' event, never a dispatch seam (#4216).

Gate 17 was founded (#1488) on the premise that ``TaskCreated`` is "the one seam
the harness Workflow/Task fan-out does NOT bypass". It is not. On the installed
binary the event has exactly ONE producer — the ``TaskCreate`` tool body — so an
``Agent``/``Task``/Workflow sub-agent fan-out never reaches it, and
``teammate_name``/``team_name`` carry the CREATING session's ambient agent
identity rather than anything about a dispatch target.

**Wording is never the proof.** An assertion and its negation share the whole
vocabulary, so no lexical predicate over that vocabulary separates them —
measured by execution, not assumed: the ``fanned-out`` ban is GREEN on both
recorded residuals, widening it to ``fan-out`` is RED on the correct prose
(which is REQUIRED to say fan-out), and a sentence-scoped polarity rule is GREEN
on both again. Three layers follow from that:

BEHAVIOUR (load-bearing) — every handler the router registers on the event,
enumerated FROM the registration so a new one is covered the day it is added,
leaves a bare local todo alone: no deny, no output, no admission seat.

INVENTORY — the registered chain and the set of tracked files that mention the
event are both pinned, so a new handler or a new describing file fails the build
until a human reads the claim and adds it deliberately.

TRIPWIRE (cheap, and NOT the proof) — the ``fanned-out`` adjective ban, whose
control fixture carries the shapes it must catch, the correct prose it must not
fire on, and — named as such — the residual shapes it provably cannot see.
"""

import io
import json
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Final, NamedTuple
from unittest.mock import patch

import pytest
from django.test import TestCase

import hooks.scripts.hook_router as router
from teatree.core import dispatch_admission as dispatch_admission_mod
from teatree.core.admission_governor import BRAKE_LOAD_PER_CORE, MachineSignal, QuotaSignal
from teatree.core.models import InteractiveDispatch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTERNALS_DOC = _REPO_ROOT / "docs" / "claude-code-internals.md"
_REPO_SKILLS_DIR = _REPO_ROOT / "skills"

#: The event whose scope the whole invariant is about.
_ANCHOR = "TaskCreated"

#: Chars either side of an anchor that count as "describing the event". Wide enough
#: to span a wrapped docstring sentence, narrow enough that a neighbouring paragraph
#: about an unrelated gate cannot bleed in.
_RADIUS = 300

#: Retired vocabulary. Naming a ``TaskCreated`` payload "fanned-out" IS the false
#: claim — the event cannot carry one. A tripwire, not the proof (module docstring).
_RETIRED_VOCABULARY = ("fanned-out", "fanned out")

#: The retired predicate names. Each read as a dispatch test while reading the
#: creator's identity — the same false claim stated where a reader checks least.
_RETIRED_PREDICATES = ("is_subagent_dispatch", "has_teammate_identity")

#: Substrings whose conjunction is the fact the correction rests on. Split so a
#: rewording of the sentence does not fail the guard, while deleting the claim does.
_PRODUCER_FACT = ("ONE producer", "TaskCreate` tool body")

#: The handlers the router registers on the event, by name. Pinned rather than
#: counted: adding one is a claim about what the event carries, so it fails the
#: build until a human reads the claim.
_REGISTERED_CHAIN: Final[tuple[str, ...]] = ("handle_dispatch_prompt_quote_scanner_on_task_create",)

#: Every tracked file that mentions the event, repo-relative. A NEW one fails the
#: build until added deliberately — a subset assertion cannot detect a new file
#: carrying the retired premise, which is how the previous walk shipped blind.
_ANCHOR_FILE_INVENTORY: Final[frozenset[str]] = frozenset(
    {
        "BLUEPRINT.md",
        "docs/blueprint/configuration.md",
        "docs/blueprint/factory-architecture.md",
        "docs/claude-code-internals.md",
        "hooks/CLAUDE.md",
        "hooks/hooks.json",
        "hooks/scripts/dispatch_admission_gate.py",
        "hooks/scripts/dispatch_seat_release.py",
        "hooks/scripts/hook_router.py",
        "hooks/scripts/run-hook.sh",
        "hooks/scripts/task_created_deny.py",
        "skills/checking/SKILL.md",
        "src/teatree/eval/session_transcript.py",
        "tests/conformance/test_consumer_caller_walk.py",
        "tests/quality/deferred_import_pegs.toml",
        "tests/quality/test_no_dead_plan_gate_refs.py",
        "tests/quality/test_no_flat_core_regrowth.py",
        "tests/teatree_hooks/test_hook_router_dispatch_quote_scanner.py",
        "tests/teatree_hooks/test_run_hook_outage_is_loud.py",
        "tests/test_gate_liveness_corpus.py",
        "tests/test_lockout_regression_corpus.py",
        "tests/test_skill_loading_code_scope.py",
        "tests/test_task_created_deny.py",
    }
)


def _tracked_files() -> list[Path]:
    """Tracked files, minus this one — the guard must be free to name what it forbids.

    Never ``check=True``: a git failure at COLLECTION time is an ERROR carrying no
    stderr, which reads as a broken harness rather than the failing invariant it is.
    """
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 — repo-relative git, no user input
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, f"`git ls-files` failed in {_REPO_ROOT}: {out.stderr.strip()}"
    here = Path(__file__).resolve()
    tracked = (_REPO_ROOT / line for line in out.stdout.splitlines() if line)
    return [path for path in tracked if path.is_file() and path.resolve() != here]


def _windows(text: str, anchor: str, radius: int) -> list[str]:
    """Every ``±radius`` window around an ``anchor`` occurrence, lowercased."""
    low = text.lower()
    found: list[str] = []
    start = 0
    while (i := low.find(anchor.lower(), start)) != -1:
        found.append(low[max(0, i - radius) : i + len(anchor) + radius])
        start = i + 1
    return found


def _tripwire_offenders(text: str) -> list[str]:
    """Anchor windows in *text* that call the payload by the retired adjective."""
    return [w for w in _windows(text, _ANCHOR, _RADIUS) if any(term in w for term in _RETIRED_VOCABULARY)]


def _files_describing_the_event() -> list[Path]:
    """Every tracked file that mentions the event at all."""
    return sorted(p for p in _tracked_files() if _ANCHOR in p.read_text(encoding="utf-8", errors="ignore"))


def _local_todo() -> dict:
    """A top-level session's OWN task-list entry — the payload every handler sees.

    No teammate fields, a description naming no skill: the shape the retired
    demand was unsatisfiable on, and the shape the retired seat booked an agent for.
    """
    return {
        "session_id": "sess-conformance",
        "hook_event_name": _ANCHOR,
        "task_id": "task-1",
        "task_subject": "rebase the branch",
        "task_description": "Bring the branch up to date and re-run the affected lane.",
    }


def _drive_the_chain() -> list[tuple[str, bool | None, str, str]]:
    """Run every registered handler on a bare local todo; return its full outcome.

    Each tuple is ``(name, verdict, stdout, stderr)``. Stdout is where a deny
    envelope lands and stderr ABORTS the event, so both are part of the contract.
    """
    outcomes: list[tuple[str, bool | None, str, str]] = []
    for handler in router._HANDLERS[_ANCHOR]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            verdict = handler(_local_todo())
        outcomes.append((handler.__name__, verdict, out.getvalue(), err.getvalue()))
    return outcomes


@pytest.fixture
def pending_demand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A non-empty, RESOLVABLE ``<session>.pending`` — the demand a gate could act on.

    Anti-vacuity: an UNRESOLVABLE pending name is dropped as stale (fail-open), so a
    handler that denies on a real demand would still read green against one.
    """
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (router.STATE_DIR / "sess-conformance.pending").write_text("code\n", encoding="utf-8")
    (router.STATE_DIR / "sess-conformance.skills").write_text("", encoding="utf-8")
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(_REPO_SKILLS_DIR))
    yield
    router.STATE_DIR = original


class TestWindowsHelper:
    """Anti-vacuity for the scanner the tripwire rests on."""

    def test_every_occurrence_gets_its_own_window(self) -> None:
        assert len(_windows("x A y A z", "a", radius=1)) == 2

    def test_a_window_is_clipped_at_the_text_edges(self) -> None:
        assert _windows("ab", "a", radius=99) == ["ab"]

    def test_an_absent_anchor_yields_nothing(self) -> None:
        assert _windows("nothing here", _ANCHOR, radius=10) == []

    def test_a_window_carries_the_neighbouring_words(self) -> None:
        assert _windows("a fanned-out TaskCreated payload", _ANCHOR, radius=20) == ["a fanned-out taskcreated payload"]


class TestEveryRegisteredHandlerLeavesALocalTodoAlone:
    """The load-bearing pin: a property of BEHAVIOUR, immune to wording.

    Enumerated from the live registration rather than a hand-written list, so a
    handler added tomorrow is covered the day it is added.
    """

    def test_the_chain_is_not_empty(self) -> None:
        # Without this the whole class degrades to zero cases and reads green.
        assert router._HANDLERS[_ANCHOR], "no handler registered on the event — the walk below checks nothing"

    def test_a_local_todo_is_allowed_by_every_taskcreated_handler(self, pending_demand: None) -> None:
        denied = [name for name, verdict, _out, _err in _drive_the_chain() if verdict is True]
        assert denied == [], f"handler(s) denied a plain task-list entry: {denied}"

    def test_no_handler_writes_a_deny_envelope_for_a_local_todo(self, pending_demand: None) -> None:
        wrote = {name: out for name, _v, out, _err in _drive_the_chain() if out.strip()}
        assert wrote == {}, f"handler(s) emitted a payload for a plain task-list entry: {wrote}"

    def test_no_handler_writes_stderr_for_a_local_todo(self, pending_demand: None) -> None:
        # The harness ABORTS task creation on ANY TaskCreated handler stderr, so a
        # diagnostic there is a lockout rather than a diagnostic.
        noisy = {name: err for name, _v, _out, err in _drive_the_chain() if err.strip()}
        assert noisy == {}, f"handler(s) wrote stderr, which aborts the event: {noisy}"


# ast-grep-ignore: ac-django-no-pytest-django-db
class TestATaskListEntryBooksNoAdmissionSeat(TestCase):
    """A todo puts no agent on the box, so it must take no seat from the ceiling.

    The governor's signals are pinned HEALTHY: a braked verdict returns before the
    seat write, so an unpinned brake would let this pass against the booking code.
    """

    def setUp(self) -> None:
        self._stack = ExitStack()
        self._stack.enter_context(patch.object(dispatch_admission_mod, "governor_enabled", return_value=True))
        self._stack.enter_context(
            patch.object(
                dispatch_admission_mod,
                "read_quota_signal",
                return_value=QuotaSignal(
                    fresh=True,
                    all_accounts_exhausted=False,
                    weekly_utilization=0.1,
                    short_utilization=0.1,
                    seconds_to_weekly_reset=7 * 24 * 3600 * 0.5,
                ),
            )
        )
        self._stack.enter_context(
            patch.object(
                dispatch_admission_mod,
                "read_machine_signal",
                return_value=MachineSignal(cores=8, load1=BRAKE_LOAD_PER_CORE, ram_available_gb=20.0),
            )
        )
        self.addCleanup(self._stack.close)

    def test_the_chain_records_no_interactive_dispatch_row(self) -> None:
        _drive_the_chain()
        seats = list(InteractiveDispatch.objects.values_list("session_id", flat=True))
        assert seats == [], f"a task-list entry booked {len(seats)} admission seat(s): {seats}"


class TestTheSurfaceInventoryIsPinned:
    """A new handler or a new describing file forces a human read of the claim."""

    def test_the_taskcreated_handler_chain_is_exactly(self) -> None:
        registered = tuple(h.__name__ for h in router._HANDLERS[_ANCHOR])
        assert registered == _REGISTERED_CHAIN, (
            "the TaskCreated chain changed. Every handler on this event governs a "
            "task-LIST entry, never a dispatch — confirm that, then update "
            f"_REGISTERED_CHAIN. registered={registered}"
        )

    def test_the_anchor_file_inventory_is_pinned(self) -> None:
        found = {p.relative_to(_REPO_ROOT).as_posix() for p in _files_describing_the_event()}
        added = sorted(found - _ANCHOR_FILE_INVENTORY)
        dropped = sorted(_ANCHOR_FILE_INVENTORY - found)
        assert (added, dropped) == ([], []), (
            f"files describing {_ANCHOR} changed — read the new sentence(s) before pinning them. "
            f"added={added} dropped={dropped}"
        )


class TripwireCase(NamedTuple):
    """One sentence and the tripwire verdict it is MEASURED to produce."""

    label: str
    sentence: str
    is_red: bool


#: Verbatim prose kept as data, so the tripwire's reach is measured rather than
#: asserted. The blind-spot rows are the point: both state the retired premise and
#: neither carries a banned adjective, so no lexical ban reaches them.
_TRIPWIRE_CASES: Final[tuple[TripwireCase, ...]] = (
    TripwireCase(
        "residual-the-retired-adjective",
        "The gate scans a fanned-out TaskCreated payload before the entry is created.",
        is_red=True,
    ),
    TripwireCase(
        "residual-the-retired-adjective-unhyphenated",
        "A fanned out task reaching TaskCreated is the premise this ticket retires.",
        is_red=True,
    ),
    TripwireCase(
        "correct-the-single-producer-fact",
        (
            "``TaskCreated`` has exactly ONE producer — the ``TaskCreate`` tool body — so "
            "every payload is an entry in some session's own task list; an "
            "``Agent``/``Task``/Workflow sub-agent fan-out never reaches this event."
        ),
        is_red=False,
    ),
    TripwireCase(
        "correct-the-governor-has-nothing-to-admit",
        (
            "The task-LIST tools bypass ``PreToolUse``, but their ``TaskCreated`` event has "
            "ONE producer, so no dispatch reaches it and the governor has nothing to admit "
            "or brake there."
        ),
        is_red=False,
    ),
    TripwireCase(
        "blindspot-assertion-the-tripwire-cannot-see",
        (
            "The harness Workflow/Task fan-out — where dispatch prompts are actually "
            "created — BYPASSES ``PreToolUse``, so that gate never fires on the real "
            "dispatch path. This ``TaskCreated`` counterpart closes that bypass."
        ),
        is_red=False,
    ),
    TripwireCase(
        "blindspot-assertion-with-a-stray-negator",
        (
            "Distinct from the SEPARATE ``Task``/``Workflow`` fan-out vehicle, which "
            "genuinely bypasses ``PreToolUse`` and fires ``TaskCreated`` — no "
            "``run_in_background`` in that schema."
        ),
        is_red=False,
    ),
)


class TestTheTripwireDiscriminates:
    """What the cheap ban catches, what it must not fire on, and what it cannot see."""

    @pytest.mark.parametrize("case", _TRIPWIRE_CASES, ids=[case.label for case in _TRIPWIRE_CASES])
    def test_the_tripwire_verdict_is_measured(self, case: TripwireCase) -> None:
        assert bool(_tripwire_offenders(case.sentence)) is case.is_red

    def test_the_blind_spots_are_recorded_as_such(self) -> None:
        # A shrinking blind-spot list is welcome; losing it silently is not — an
        # empty one would read as "the tripwire sees everything", which it does not.
        assert [c for c in _TRIPWIRE_CASES if c.label.startswith("blindspot-")], (
            "the recorded blind spots are gone — either the tripwire genuinely covers "
            "them now (state how in the docstring) or the control was quietly emptied"
        )


class TestNoSurfaceCallsTheEventADispatch:
    @pytest.mark.parametrize("path", _files_describing_the_event(), ids=lambda p: p.name)
    def test_no_event_mention_uses_the_retired_vocabulary(self, path: Path) -> None:
        offenders = _tripwire_offenders(path.read_text(encoding="utf-8"))
        assert offenders == [], f"{path.name} calls a {_ANCHOR} payload fanned-out: …{offenders[0]}…"


class TestTheRetiredPredicateNamesAreGone:
    @pytest.mark.parametrize("predicate", _RETIRED_PREDICATES)
    def test_no_tracked_file_still_names_the_retired_predicate(self, predicate: str) -> None:
        # Catches a stale import, a stale patch-target string, and a stale doc
        # reference — the patch-target one fails vacuously rather than loudly.
        stale = [
            path.relative_to(_REPO_ROOT).as_posix()
            for path in _tracked_files()
            if predicate in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert stale == [], f"stale '{predicate}' reference(s): {stale}"


class TestTheCanonicalDocStillCarriesTheFact:
    def test_the_internals_doc_states_the_single_producer(self) -> None:
        text = _INTERNALS_DOC.read_text(encoding="utf-8")
        missing = [fragment for fragment in _PRODUCER_FACT if fragment not in text]
        assert missing == [], f"{_INTERNALS_DOC.name} no longer states the producer fact: {missing}"


class TestTheDenyEmitterIsUnchangedAndStillReachable:
    """The verified-correct deny rendering survives the retirement (#4216 review)."""

    def test_a_deny_carries_its_reason_on_the_field_the_consumer_reads(self) -> None:
        from hooks.scripts.task_created_deny import (  # noqa: PLC0415 — deferred: keep the module top free of the gate under test
            build_deny_payload,
            harness_surfaced_deny_text,
        )

        payload = build_deny_payload("load /t3:code first")
        assert harness_surfaced_deny_text(payload, "", exit_code=2) == "load /t3:code first"
        assert json.loads(json.dumps(payload))["decision"] == "block"
