# test-path: cross-cutting — a whole-tree harness-contract invariant; no src/teatree/ mirror.
""":data:`TaskCreated` is the task-LIST tools' event, never a dispatch seam (#4216).

Gate 17 was founded (#1488) on the premise that ``TaskCreated`` is "the one seam
the harness Workflow/Task fan-out does NOT bypass". It is not. On the installed
binary the event has exactly ONE producer — the ``TaskCreate`` tool body — so an
``Agent``/``Task``/Workflow sub-agent fan-out never reaches it, and
``teammate_name``/``team_name`` carry the CREATING session's ambient agent
identity rather than anything about a dispatch target.

**Wording was never the proof, and a ban over wording was never the guard.** A
lexical ban asks "does this sentence contain word W" and is silent on every other
sentence, so a rephrasing walks past it — measured three times here, three
residuals, one mechanism. It is retired. Three layers replace it:

BEHAVIOUR (load-bearing) — every handler the router registers on the event,
enumerated FROM the registration, leaves a task-list entry alone: no deny, no
output, no admission seat. Driven over the event's SCHEMA rather than one
example — see :data:`_EVENT_SCHEMA` for the rule and why.

INVENTORY — the registered chain, the set of tracked files that mention the
event, and every handler name those files spell near it are all pinned or derived
from the live registration, so a new handler, a new describing file, or a handler
name that has died fails the build until a human reads the claim.

LEDGER — the doc-surface prose is content-addressed in
``tests/quality/anchor_prose_pegs.toml`` (the gate is
``tests/quality/test_anchor_prose_pegs.py``). Its scope is a LIMIT, not coverage,
and the limit is a ±400-char window around a LITERAL occurrence — 8.1% of
``hooks/CLAUDE.md`` when last measured, pinned per file by its ``coverage``
table, which is where to read the live number rather than this sentence. It
forces a human to re-read a CHANGED sentence; it does not verify the sentence is
true, it does not cover test-file prose, and it cannot see a file that never
spells the event at all.

DENY TEXT — that last blind spot is what the fifth residual lived in (#4381): the
renderer of a handler's operator-visible refusal contains no occurrence of the
event, so no radius reaches it. :class:`TestEveryRegisteredHandlersDenyTextIsPinned`
drives each registered handler's DENY path and content-addresses what the caller
is actually shown — derived from the registration, so it needs no literal.
"""

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from itertools import product
from pathlib import Path
from typing import Final
from unittest.mock import patch

import pytest
from django.test import TestCase

import hooks.scripts.hook_router as router
from teatree.core import dispatch_admission as dispatch_admission_mod
from teatree.core.admission_governor import BRAKE_LOAD_PER_CORE, MachineSignal, QuotaSignal
from teatree.core.models import InteractiveDispatch
from tests._generated_artifacts import DURATIONS_CASSETTE
from tests._git_repo import make_git_repo, run_git

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTERNALS_DOC = _REPO_ROOT / "docs" / "claude-code-internals.md"
_REPO_SKILLS_DIR = _REPO_ROOT / "skills"

#: The event whose scope the whole invariant is about.
_ANCHOR = "TaskCreated"

#: Chars either side of an anchor that count as "describing the event". Kept equal to
#: the ledger's own radius (pinned from the other side, so drift is loud). 400 rather
#: than 300 because at 300 the load-bearing hooks/CLAUDE.md bullet's own handler name
#: sat 327 chars from its nearest anchor — outside every window, so neither instrument
#: covered it.
_RADIUS = 400

#: The event's stdin schema, pinned as DATA (hooks/CLAUDE.md § TaskCreated; the
#: harness re-check grep is in docs/claude-code-internals.md).
#:
#: THE RULE a behaviour pin follows, recorded once here rather than re-derived per
#: case: drive the event's SCHEMA, not one convenient example. Every OPTIONAL field
#: is driven present AND absent, because an optional field is exactly where a
#: handler hides a discriminator a single example never exercises. And for a
#: RETIREMENT specifically, every field the retired premise READ is driven at the
#: value that would have FIRED it — otherwise the pin proves only that the handler
#: ignores inputs nobody ever keyed on.
_EVENT_SCHEMA: Final[dict[str, tuple[str, ...]]] = {
    "required": ("session_id", "task_id", "task_subject", "task_description"),
    "optional": ("teammate_name", "team_name"),
}

#: The ambient agent identity the retired dispatch-ness predicate read. Driving the
#: optional fields at THIS value is what makes the matrix a behaviour pin rather
#: than a wording one.
_AMBIENT_IDENTITY: Final[dict[str, str]] = {"teammate_name": "t3:coder", "team_name": "factory"}

#: Symbols the correction retired. A stale-SYMBOL sweep — a dead import, a dead
#: ``patch()`` target string (which fails VACUOUSLY rather than loudly), a dead doc
#: reference. Never a behaviour ban: the behaviour is the matrix above.
_RETIRED_SYMBOLS: Final[tuple[str, ...]] = (
    "is_subagent_dispatch",
    "has_teammate_identity",
    "handle_enforce_skill_loading_on_task_create",
    "handle_dispatch_admission_on_task_create",
    "subagent_skill_gate",
    "_task_text_skip_token",
    "skip-skill-gate",
)

#: Substrings whose conjunction is the fact the correction rests on. Split so a
#: rewording of the sentence does not fail the guard, while deleting the claim does.
_PRODUCER_FACT = ("ONE producer", "TaskCreate` tool body")

#: The handlers the router registers on the event, by name. Pinned rather than
#: counted: adding one is a claim about what the event carries, so it fails the
#: build until a human reads the claim.
_REGISTERED_CHAIN: Final[tuple[str, ...]] = ("handle_dispatch_prompt_quote_scanner_on_task_create",)

_HANDLER_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"handle_[a-z0-9_]+")

#: Files whose JOB is to name a handler that no longer exists, so the liveness
#: cross-check below cannot apply to them.
_DEAD_NAME_LEDGERS: Final[frozenset[str]] = frozenset({"tests/quality/test_no_dead_plan_gate_refs.py"})

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
        "hooks/scripts/hook_router.py",
        "hooks/scripts/run-hook.sh",
        "hooks/scripts/task_created_deny.py",
        "skills/checking/SKILL.md",
        "src/teatree/eval/session_transcript.py",
        "tests/conformance/test_consumer_caller_walk.py",
        "tests/quality/anchor_prose_pegs.toml",
        "tests/quality/deferred_import_pegs.toml",
        "tests/quality/test_anchor_prose_pegs.py",
        "tests/quality/test_no_dead_plan_gate_refs.py",
        "tests/quality/test_no_flat_core_regrowth.py",
        "tests/teatree_hooks/test_hook_router_dispatch_quote_scanner.py",
        "tests/teatree_hooks/test_run_hook_outage_is_loud.py",
        "tests/test_gate_liveness_corpus.py",
        "tests/test_lockout_regression_corpus.py",
        "tests/test_hook_router_task_created_never_blocks.py",
        "tests/test_skill_loading_code_scope.py",
        "tests/test_task_created_deny.py",
    }
)


def _tracked_files(repo_root: Path = _REPO_ROOT) -> list[Path]:
    """Tracked files, minus this one — the guard must be free to name what it forbids.

    Never ``check=True``: a git failure at COLLECTION time is an ERROR carrying no
    stderr, which reads as a broken harness rather than the failing invariant it is.
    """
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 — repo-relative git, no user input
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, f"`git ls-files` failed in {repo_root}: {out.stderr.strip()}"
    # The durations cassette goes too: it records node ids, so it spells every symbol
    # scanned for below without referencing any of them.
    exempt = {Path(__file__).resolve(), (repo_root / DURATIONS_CASSETTE).resolve()}
    tracked = (repo_root / line for line in out.stdout.splitlines() if line)
    return [path for path in tracked if path.is_file() and path.resolve() not in exempt]


def _anchor_spans(text: str, anchor: str, radius: int) -> list[tuple[int, int]]:
    """``(start, end)`` of every ``±radius`` window around *anchor*, overlaps merged.

    Spans rather than substrings so an identifier scan can match against the WHOLE
    text: a name clipped by a window edge would otherwise read as a dead symbol.
    """
    merged: list[tuple[int, int]] = []
    start = 0
    while (i := text.find(anchor, start)) != -1:
        lo, hi = max(0, i - radius), min(len(text), i + len(anchor) + radius)
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
        start = i + 1
    return merged


def _handler_names_near_the_anchor(text: str) -> set[str]:
    """Complete ``handle_*`` identifiers overlapping an anchor window in *text*."""
    spans = _anchor_spans(text, _ANCHOR, _RADIUS)
    return {
        match.group()
        for match in _HANDLER_IDENT_RE.finditer(text)
        if any(lo < match.end() and match.start() < hi for lo, hi in spans)
    }


def _files_describing_the_event() -> list[Path]:
    """Every tracked file that mentions the event at all."""
    return sorted(p for p in _tracked_files() if _ANCHOR in p.read_text(encoding="utf-8", errors="ignore"))


def _registered_handler_names() -> set[str]:
    """Every handler the router registers, on any event."""
    return {handler.__name__ for handlers in router._HANDLERS.values() for handler in handlers}


def _local_todos() -> list[dict[str, str]]:
    """The event's full optional-field matrix — a session's OWN task-list entries.

    Subject and description stay innocuous so the surviving quote scanner is not
    legitimately tripped; the pending demand below is what the retired skill-loading
    arm keyed on, and the ambient identity what the retired dispatch-ness one read.
    """
    base = {
        "session_id": "sess-conformance",
        "task_id": "task-1",
        "task_subject": "rebase the branch",
        "task_description": "Bring the branch up to date and re-run the affected lane.",
    }
    rows: list[dict[str, str]] = []
    for present in product((False, True), repeat=len(_EVENT_SCHEMA["optional"])):
        extra = {
            field: _AMBIENT_IDENTITY[field]
            for field, is_present in zip(_EVENT_SCHEMA["optional"], present, strict=True)
            if is_present
        }
        rows.append(base | extra)
    return rows


def _drive_the_chain() -> list[tuple[str, dict[str, str], bool | None, str, str]]:
    """Run every registered handler over every matrix row; return each full outcome.

    Each tuple is ``(name, payload, verdict, stdout, stderr)``. Stdout is where a deny
    envelope lands and stderr ABORTS the event, so both are part of the contract.
    """
    outcomes: list[tuple[str, dict[str, str], bool | None, str, str]] = []
    for handler in router._HANDLERS[_ANCHOR]:
        for payload in _local_todos():
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                verdict = handler(dict(payload))
            outcomes.append((handler.__name__, payload, verdict, out.getvalue(), err.getvalue()))
    return outcomes


def _label(name: str, payload: dict[str, str]) -> str:
    driven = ",".join(f for f in _EVENT_SCHEMA["optional"] if f in payload) or "no-optional-fields"
    return f"{name}[{driven}]"


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


class TestTheMatrixCoversTheSchema:
    """A field the schema gains tomorrow fails the build until it is driven."""

    def test_every_schema_field_is_driven(self) -> None:
        driven = {key for payload in _local_todos() for key in payload}
        assert driven == set(_EVENT_SCHEMA["required"]) | set(_EVENT_SCHEMA["optional"])

    def test_each_optional_field_is_driven_both_absent_and_present(self) -> None:
        for field in _EVENT_SCHEMA["optional"]:
            values = [payload.get(field) for payload in _local_todos()]
            assert None in values, f"{field} is never driven ABSENT"
            assert _AMBIENT_IDENTITY[field] in values, f"{field} is never driven at the ambient identity"

    def test_the_matrix_is_the_full_cross_product(self) -> None:
        assert len(_local_todos()) == 2 ** len(_EVENT_SCHEMA["optional"])


class TestEveryRegisteredHandlerLeavesALocalTodoAlone:
    """The load-bearing pin: a property of BEHAVIOUR, immune to wording.

    Enumerated from the live registration rather than a hand-written list, so a
    handler added tomorrow is covered the day it is added, and driven over the whole
    schema so a handler keying on an optional field cannot hide behind one example.
    """

    def test_the_chain_is_not_empty(self) -> None:
        # Without this the whole class degrades to zero cases and reads green.
        assert router._HANDLERS[_ANCHOR], "no handler registered on the event — the walk below checks nothing"

    def test_a_local_todo_is_allowed_by_every_taskcreated_handler(self, pending_demand: None) -> None:
        denied = [_label(name, payload) for name, payload, verdict, _o, _e in _drive_the_chain() if verdict is True]
        assert denied == [], f"handler(s) denied a plain task-list entry: {denied}"

    def test_no_handler_writes_a_deny_envelope_for_a_local_todo(self, pending_demand: None) -> None:
        wrote = {_label(n, p): out for n, p, _v, out, _e in _drive_the_chain() if out.strip()}
        assert wrote == {}, f"handler(s) emitted a payload for a plain task-list entry: {wrote}"

    def test_no_handler_writes_stderr_for_a_local_todo(self, pending_demand: None) -> None:
        # The harness ABORTS task creation on ANY TaskCreated handler stderr, so a
        # diagnostic there is a lockout rather than a diagnostic.
        noisy = {_label(n, p): err for n, p, _v, _o, err in _drive_the_chain() if err.strip()}
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


class TestEveryHandlerNamedNearTheEventIsLive:
    """Derived from the live registration, so a name fails the day its handler dies.

    Deliberately NOT "must ride TaskCreated": the anchor windows legitimately name
    handlers on other events (the router's own registered-event list is one), so
    that stricter set is red on correct prose. Which handlers ride the event is
    already pinned exactly, from the registration, by ``_REGISTERED_CHAIN`` above.
    """

    @pytest.mark.parametrize("path", _files_describing_the_event(), ids=lambda p: p.name)
    def test_no_dead_handler_name_sits_next_to_the_event(self, path: Path) -> None:
        if path.relative_to(_REPO_ROOT).as_posix() in _DEAD_NAME_LEDGERS:
            pytest.skip("this file's job is to ban a handler name that no longer exists")
        named = _handler_names_near_the_anchor(path.read_text(encoding="utf-8", errors="ignore"))
        dead = sorted(named - _registered_handler_names())
        assert dead == [], f"{path.name} names handler(s) the router no longer registers: {dead}"


class TestTheAnchorSpanHelper:
    """Anti-vacuity for the scanner the cross-check above rests on."""

    def test_each_isolated_occurrence_gets_its_own_span(self) -> None:
        assert len(_anchor_spans("A" + "." * 50 + "A", "A", radius=5)) == 2

    def test_overlapping_spans_merge(self) -> None:
        assert _anchor_spans("A.A", "A", radius=5) == [(0, 3)]

    def test_an_absent_anchor_yields_nothing(self) -> None:
        assert _anchor_spans("nothing here", _ANCHOR, radius=10) == []

    def test_an_identifier_clipped_by_a_window_edge_is_not_reported(self) -> None:
        # The failure this forecloses: a live handler whose name runs off the window
        # edge reads as a dead symbol, and the cross-check reds on correct prose.
        text = f"{_ANCHOR}{'.' * _RADIUS}handle_dispatch_prompt_quote_scanner_on_task_create"
        assert _handler_names_near_the_anchor(text) == set()

    def test_an_identifier_inside_a_window_is_reported(self) -> None:
        assert _handler_names_near_the_anchor(f"{_ANCHOR} handle_thing") == {"handle_thing"}


@dataclasses.dataclass(frozen=True)
class DenyDriver:
    """What it takes to drive ONE registered handler down its DENY path.

    Held per handler NAME so the totality test below derives its own obligation from
    the live registration rather than from a hand-written list of modules.
    """

    #: DB-home flags seeded into a hermetic config store — the REAL resolution path,
    #: not a patch of the handler's private predicate.
    settings: Mapping[str, bool]
    #: The event payload that must trip the gate. Synthetic user-voice SHAPES only.
    payload: Mapping[str, str]


_DENY_DRIVERS: Final[dict[str, DenyDriver]] = {
    "handle_dispatch_prompt_quote_scanner_on_task_create": DenyDriver(
        settings={"dispatch_quote_gate_on_task_create_enabled": True},
        payload={
            "session_id": "sess-deny-driver",
            "task_id": "task-deny-1",
            "task_subject": "## User ask (verbatim, 2026-05-20)",
            "task_description": "Implement the export endpoint and wire it to the dashboard.",
        },
    ),
}

#: The operator-visible deny text each driver produces, content-addressed. A digest
#: cannot tell TRUE from FALSE; its job is to force a human to READ the reason when it
#: moves. Unlike the prose ledger it needs no literal anchor in the file that renders
#: the text, which is why it reaches the shared renderer the ledger is blind to.
_DENY_TEXT_PEGS: Final[dict[str, str]] = {
    "handle_dispatch_prompt_quote_scanner_on_task_create": "92b49cdd9eaa8351",
}

#: Phrases asserting the premise this event's correction retired. The peg above is the
#: guard; this names the exact shape that fired five times, so a reintroduction fails
#: with its reason rather than with a moved hash.
_RETIRED_PREMISE_PHRASES: Final[tuple[str, ...]] = (
    "pre-dispatch",
    "Agent/Task prompt",
    "before dispatching",
    "sub-agent",
)


@contextlib.contextmanager
def _gate_live(driver: DenyDriver, tmp_path: Path) -> Iterator[None]:
    """Seed *driver*'s flags into a hermetic config store, ledger pinned under *tmp_path*."""
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
            [(key, json.dumps(value)) for key, value in driver.settings.items()],
        )
        conn.commit()
    finally:
        conn.close()
    with patch.dict(os.environ, {"T3_CONFIG_DB": str(db), "T3_DATA_DIR": str(tmp_path)}):
        yield


def _drive_the_deny(name: str, driver: DenyDriver, tmp_path: Path) -> tuple[bool | None, str, str]:
    """Run one registered handler under *driver*; return ``(verdict, stdout, stderr)``."""
    handler = next(h for h in router._HANDLERS[_ANCHOR] if h.__name__ == name)
    out, err = io.StringIO(), io.StringIO()
    with _gate_live(driver, tmp_path), redirect_stdout(out), redirect_stderr(err):
        verdict = handler(dict(driver.payload))
    return verdict, out.getvalue(), err.getvalue()


def _surfaced_deny_text(stdout: str, stderr: str) -> str:
    """What the harness would actually show the caller for this deny."""
    from hooks.scripts.task_created_deny import (  # noqa: PLC0415 — deferred: keep the module top free of the gate under test
        DENY_EXIT_CODE,
        harness_surfaced_deny_text,
    )

    payload = json.loads(stdout) if stdout.strip() else {}
    return harness_surfaced_deny_text(payload, stderr, exit_code=DENY_EXIT_CODE)


class TestEveryRegisteredHandlersDenyTextIsPinned:
    """The literal-free half: the surface is DERIVED from the registration (#4381).

    The prose ledger keys on a LITERAL occurrence of the anchor, so a file that never
    spells it is invisible to it however wide the radius grows — and the renderer that
    produced the fifth residual is such a file. What is reachable without a literal is
    the OUTPUT of the handlers the router registers, so that is what is pinned here.
    """

    def test_every_registered_handler_has_a_deny_driver(self) -> None:
        # The derivation seam: a handler added tomorrow fails the build until a human
        # supplies the driver that makes its deny text observable.
        assert set(_DENY_DRIVERS) == set(_REGISTERED_CHAIN), (
            "the TaskCreated chain and the deny-driver set disagree. Every registered handler "
            "needs a driver, or its operator-visible deny text is pinned by nothing. "
            f"drivers={sorted(_DENY_DRIVERS)} registered={sorted(_REGISTERED_CHAIN)}"
        )

    def test_every_driver_has_a_pegged_deny_text(self) -> None:
        assert set(_DENY_TEXT_PEGS) == set(_DENY_DRIVERS)

    @pytest.mark.parametrize("name", sorted(_DENY_DRIVERS))
    def test_the_deny_path_actually_denies(self, name: str, tmp_path: Path) -> None:
        # Anti-vacuity: emit_task_create_deny fails OPEN on a reason that would not
        # reach the caller, so "stdout is non-empty" alone reads green on a broken deny.
        verdict, out, err = _drive_the_deny(name, _DENY_DRIVERS[name], tmp_path)
        assert verdict is True, f"{name} did not deny its driver payload"
        assert _surfaced_deny_text(out, err), f"{name} denied with a reason the caller never sees"

    @pytest.mark.parametrize("name", sorted(_DENY_DRIVERS))
    def test_the_driver_is_what_makes_the_deny_happen(self, name: str, tmp_path: Path) -> None:
        # Control: strip the settings and the same payload is inert, so the pins above
        # are decided by the driver rather than by an ambient default.
        inert = dataclasses.replace(_DENY_DRIVERS[name], settings={})
        verdict, out, err = _drive_the_deny(name, inert, tmp_path)
        assert verdict is not True
        assert _surfaced_deny_text(out, err) == ""

    @pytest.mark.parametrize("name", sorted(_DENY_DRIVERS))
    def test_the_operator_visible_deny_text_is_pegged(self, name: str, tmp_path: Path) -> None:
        _verdict, out, err = _drive_the_deny(name, _DENY_DRIVERS[name], tmp_path)
        text = _surfaced_deny_text(out, err)
        assert hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] == _DENY_TEXT_PEGS[name], (
            f"{name}'s deny text moved. READ it below, confirm it describes the surface this "
            f"handler actually governs, then re-peg _DENY_TEXT_PEGS.\n{text}\n"
        )

    @pytest.mark.parametrize(
        ("name", "phrase"),
        [(name, phrase) for name in sorted(_DENY_DRIVERS) for phrase in _RETIRED_PREMISE_PHRASES],
    )
    def test_no_deny_text_asserts_the_retired_dispatch_premise(self, name: str, phrase: str, tmp_path: Path) -> None:
        _verdict, out, err = _drive_the_deny(name, _DENY_DRIVERS[name], tmp_path)
        text = _surfaced_deny_text(out, err)
        assert phrase not in text, (
            f"{name} tells the operator their task-list entry was a {phrase!r}. This event has ONE "
            f"producer, so the remedy that reason names does not exist here.\n{text}\n"
        )


class TestTheTrackedWalkExemptsTheGeneratedCassette:
    """Control: exactly one generated file is exempt, not the directory holding it."""

    @staticmethod
    def _repo_tracking(tmp_path: Path, *relative: str) -> None:
        make_git_repo(tmp_path)
        for name in relative:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{_ANCHOR} handle_thing\n")
        run_git(tmp_path, "add", "-A")

    def test_the_cassette_is_skipped_while_its_neighbour_is_still_walked(self, tmp_path: Path) -> None:
        self._repo_tracking(tmp_path, DURATIONS_CASSETTE, "dev/handwritten.py")
        walked = {p.relative_to(tmp_path).as_posix() for p in _tracked_files(tmp_path)}
        assert DURATIONS_CASSETTE not in walked
        assert "dev/handwritten.py" in walked


class TestTheRetiredSymbolsAreGone:
    @pytest.mark.parametrize("symbol", _RETIRED_SYMBOLS)
    def test_no_tracked_file_still_names_the_retired_symbol(self, symbol: str) -> None:
        stale = [
            path.relative_to(_REPO_ROOT).as_posix()
            for path in _tracked_files()
            if symbol in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert stale == [], f"stale '{symbol}' reference(s): {stale}"


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
