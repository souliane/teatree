# test-path: cross-cutting
# Exercises hooks/scripts/standing_directives_delivery.py (no src/teatree mirror); the two hook entry
# points that call it run as subprocesses in test_owner_turn_context.py and test_standing_rules_start.py.
"""The Claude-plugin delivery of the standing directives (#4166) — zero-turn context, never a registration.

Layer 2 of the harness-neutrality split. Every directive rides a turn that already
exists: all of them at session start, and each again on the first tool call after an
owner prompt once its own cadence has passed. Nothing is registered and nothing wakes
the session. The adapter carries NO directive text, NO cadence value and no policy of
its own, and every guard below ships with a CONTROL corpus proving it can go red.
"""

import json
import operator
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from hooks.scripts import owner_turn_context, standing_directives_delivery, standing_rules_start
from hooks.scripts.loop_registry_path import OWNER_LOOP
from hooks.scripts.standing_directives_delivery import due_rules
from teatree import standing_directives_cache
from teatree.loop.standing_directives import (
    MAX_DIRECTIVE_CHARS,
    SCOPE_ATTENDED,
    SCOPE_ATTENDED_SINGLETON,
    STANDING_DIRECTIVES,
    compiled_directives,
)
from teatree.standing_directives_cache import StandingDirectivePayload

_FAKE: list[StandingDirectivePayload] = [
    {"slot_id": "slot-a", "cadence_seconds": 300, "text": "Alpha rule.", "scope": SCOPE_ATTENDED},
    {"slot_id": "slot-b", "cadence_seconds": 90, "text": "Beta rule.", "scope": SCOPE_ATTENDED},
    {"slot_id": "slot-c", "cadence_seconds": 600, "text": "Gamma rule.", "scope": SCOPE_ATTENDED_SINGLETON},
]
_HOST_WIDE = "Gamma rule."

_ADAPTER_SOURCES = [
    Path(module.__file__).read_text(encoding="utf-8")
    for module in (standing_directives_delivery, owner_turn_context, standing_rules_start)
    if module.__file__
]
_DEFAULT_TEXTS = [d.default_text for d in STANDING_DIRECTIVES]


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("T3_HOOK_STATE_DIR", str(state))
    monkeypatch.setenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", str(state))
    return state


@pytest.fixture(autouse=True)
def attended_and_unengaged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the seams the ambient env opens: a factory worker's SDK lane, and the owner's autoload."""
    monkeypatch.delenv("CLAUDE_AGENT_SDK_VERSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setenv("T3_AUTOLOAD", "0")


@pytest.fixture(autouse=True)
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The loop registry, with ``sess-1`` the live session that owns the loop slot."""
    directory = tmp_path / "registry"
    directory.mkdir()
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(directory))
    path = directory / "loop-registry.json"
    _own_the_loop(path, "sess-1")
    return path


def _own_the_loop(registry: Path, session_id: str, *, pid: int | None = None) -> None:
    owner = {"session_id": session_id, "pid": os.getpid() if pid is None else pid}
    registry.write_text(json.dumps({OWNER_LOOP: owner}), encoding="utf-8")


@pytest.fixture
def engaged(state_dir: Path) -> None:
    (state_dir / "sess-1.teatree-active").touch()


@pytest.fixture
def published(engaged: None) -> None:
    standing_directives_cache.write(_FAKE)


def _deliver(session_id: str, *, every_slot: bool) -> str:
    """What the session is handed, recorded as delivered — as a hook does once its write succeeded."""
    rules = due_rules(session_id, every_slot=every_slot)
    rules.mark_delivered()
    return rules.text


def _due(session_id: str = "sess-1") -> str:
    return _deliver(session_id, every_slot=False)


def _every(session_id: str = "sess-1") -> str:
    return _deliver(session_id, every_slot=True)


def _backdate(state_dir: Path, slot_id: str, seconds: float, session_id: str = "sess-1") -> None:
    marker = state_dir / f"{session_id}.directives-injected"
    stamps = json.loads(marker.read_text(encoding="utf-8"))
    stamps[slot_id] -= seconds
    marker.write_text(json.dumps(stamps), encoding="utf-8")


# ── the policy-leak check, derived from layer 1 ──────────────────────


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 3}


#: Delivery vocabulary the adapter's own prose legitimately shares with the layer-1
#: texts: the mechanism's nouns and the fail-open contract's words. Nothing here names
#: what a directive SAYS. Widening this set is a deliberate review decision — that is
#: the whole point of deriving the denylist rather than hand-picking it. ``list`` is the
#: Python builtin, and ``transcript`` is the harness's own ``transcript_path`` payload key
#: (the todo directive names the transcript for an unrelated reason).
_ADAPTER_PLUMBING = frozenset(
    {
        "already",
        "cannot",
        "cold",
        "every",
        "false",
        "first",
        "from",
        "list",
        "never",
        "none",
        "only",
        "open",
        "request",
        "session",
        "standing",
        "state",
        "then",
        "this",
        "transcript",
        "turn",
        "user",
        "with",
        "work",
    }
)


def _without_imports(source: str) -> str:
    """*source* with its import lines dropped — a module path is plumbing, never policy."""
    return "\n".join(line for line in source.splitlines() if not re.match(r"\s*(from|import)\s+[\w.]", line))


def policy_leaks(source: str, texts: Iterable[str]) -> list[str]:
    """Distinctive layer-1 policy words that appear in *source* — empty means pure."""
    distinctive = {word for text in texts for word in _words(text)} - _ADAPTER_PLUMBING
    return sorted(distinctive & _words(_without_imports(source)))


_LEAKING_SOURCES = {
    "paraphrased_header": 'stream.write("Golden rule: PLAN then IMPLEMENT then COLD REVIEW\\n")',
    "verbatim_slice": '    """Fresh merge_safe at the LIVE head with green CI → merge now via the keystone."""',
    "bare_comment": "    # maker ≠ checker, so never dispatch the reviewer that wrote the branch",
}


class TestLayerPurity:
    """A NEW policy word must be caught by default, not by hand-listing it."""

    @pytest.mark.parametrize("source", _ADAPTER_SOURCES, ids=["delivery", "turn-context", "session-start"])
    def test_the_real_adapter_carries_no_layer_one_policy_word(self, source: str) -> None:
        assert policy_leaks(source, _DEFAULT_TEXTS) == []

    @pytest.mark.parametrize("fake_source", _LEAKING_SOURCES.values(), ids=list(_LEAKING_SOURCES))
    def test_control_a_leaking_source_is_caught(self, fake_source: str) -> None:
        assert policy_leaks(fake_source, _DEFAULT_TEXTS) != []

    @pytest.mark.parametrize("source", _ADAPTER_SOURCES, ids=["delivery", "turn-context", "session-start"])
    def test_carries_no_cadence_literal_of_its_own(self, source: str) -> None:
        for cadence in ("300", "1800", "600"):
            assert cadence not in source


# ── the exact-equality round trip and its mutant corpus ──────────────

_SENTINEL_SUFFIX = "END-OF-DIRECTIVE"


def _probe_text(slot_id: str) -> str:
    """A directive at the cap, ending in a sentinel, so any truncation loses the tail."""
    sentinel = f"[{slot_id}-{_SENTINEL_SUFFIX}]."
    filler = ("Probe body for the exact-equality round trip. " * 40)[: MAX_DIRECTIVE_CHARS - len(sentinel) - 1]
    return f"{filler} {sentinel}"


_PROBES: list[StandingDirectivePayload] = [
    {"slot_id": "probe-a", "cadence_seconds": 300, "text": _probe_text("probe-a"), "scope": SCOPE_ATTENDED},
    {"slot_id": "probe-b", "cadence_seconds": 900, "text": _probe_text("probe-b"), "scope": SCOPE_ATTENDED_SINGLETON},
]

_EXPECTED_PROBE_LINES = [f"  - [{probe['slot_id']}] {probe['text']}" for probe in _PROBES]

_ROUND_TRIP_MUTANTS: dict[str, Callable[[str], str]] = {
    "truncate_200": operator.itemgetter(slice(200)),
    "truncate_1000": operator.itemgetter(slice(1000)),
    "strip_trailing_period": lambda rendered: "\n".join(line.rstrip(".") for line in rendered.split("\n")),
    "collapse_whitespace": lambda rendered: " ".join(rendered.split()),
    "upper": str.upper,
}


def _assert_round_trip(render: Callable[[str], str]) -> None:
    """Every published directive reaches the session as EXACTLY its rendered line."""
    emitted = [line for line in render("sess-1").split("\n") if line.startswith("  - ")]

    assert emitted == _EXPECTED_PROBE_LINES


class TestTheVerbatimRoundTrip:
    @pytest.fixture(autouse=True)
    def _published_probes(self, engaged: None) -> None:
        standing_directives_cache.write(_PROBES)

    def test_every_published_directive_round_trips_exactly(self) -> None:
        _assert_round_trip(_due)

    @pytest.mark.parametrize("mutant", _ROUND_TRIP_MUTANTS.values(), ids=list(_ROUND_TRIP_MUTANTS))
    def test_control_a_mutated_render_fails_the_round_trip(self, mutant: Callable[[str], str]) -> None:
        with pytest.raises(AssertionError):
            _assert_round_trip(lambda session_id: mutant(_due(session_id)))


# ── nothing is ever registered ───────────────────────────────────────

_REGISTRATION = re.compile(r"(?<![\w/])/loop\b|\bcron|\bschedulewakeup\b|\bregister", re.IGNORECASE)


def registration_instructions(rendered: str) -> list[str]:
    """Every phrase in *rendered* that asks the session to arm something recurring — empty means none."""
    return [match.group(0) for match in _REGISTRATION.finditer(rendered)]


class TestNothingIsEverRegistered:
    """The owner's rule: teatree runs the loops, so a session is never asked to arm one."""

    def test_the_real_directives_at_session_start_ask_for_no_registration(self, engaged: None) -> None:
        rendered = _every()

        assert all(directive.text in rendered for directive in compiled_directives())
        assert registration_instructions(rendered) == []

    def test_a_due_redelivery_asks_for_no_registration(self, published: None, state_dir: Path) -> None:
        _every()
        _backdate(state_dir, "slot-b", 3600)

        assert registration_instructions(_due()) == []

    @pytest.mark.parametrize(
        "rendered",
        [
            "  - /loop 10m [standing-pr-board] Drive the PR board.",
            "Run CronCreate for each slot.",
            "Session setup: register the 2 recurring standing slots.",
            "Use ScheduleWakeup to come back later.",
        ],
    )
    def test_control_a_registration_instruction_is_caught(self, rendered: str) -> None:
        assert registration_instructions(rendered) != []


# ── who gets what ────────────────────────────────────────────────────


class TestWhoGetsWhat:
    def test_a_session_engaged_by_a_teatree_skill_gets_them(self, published: None) -> None:
        assert "Alpha rule." in _due()

    def test_a_session_engaged_by_any_t3_skill_gets_them(self, state_dir: Path) -> None:
        (state_dir / "sess-2.t3-engaged").touch()
        standing_directives_cache.write(_FAKE)

        assert "Alpha rule." in _due("sess-2")

    def test_autoload_engages_every_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_AUTOLOAD", "1")
        standing_directives_cache.write(_FAKE)

        assert "Alpha rule." in _due("sess-unmarked")

    def test_an_unengaged_session_gets_nothing(self) -> None:
        standing_directives_cache.write(_FAKE)

        assert _due() == ""
        assert _every() == ""

    def test_another_sessions_marker_engages_only_that_session(self, published: None) -> None:
        assert _due("sess-2") == ""

    @pytest.mark.parametrize(
        ("name", "value"), [("CLAUDE_AGENT_SDK_VERSION", "0.1.0"), ("CLAUDE_CODE_ENTRYPOINT", "sdk-py")]
    )
    def test_the_sdk_lane_gets_nothing(
        self, published: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
    ) -> None:
        # A factory worker is FSM-governed and has no user-request channel.
        monkeypatch.setenv(name, value)

        assert _due() == ""
        assert _every() == ""

    def test_an_empty_session_id_is_a_no_op(self, published: None) -> None:
        assert _due("") == ""


# ── one session per host for a host-wide slot ────────────────────────


class TestTheHostWideSlot:
    """The PR board is one board per host, so only the session that owns the loop slot is sent to drive it."""

    def test_the_session_owning_the_loop_slot_gets_it(self, published: None) -> None:
        assert _HOST_WIDE in _due()
        assert _HOST_WIDE in _every()

    def test_any_other_engaged_session_never_gets_it(self, published: None, state_dir: Path) -> None:
        (state_dir / "sess-2.teatree-active").touch()

        assert "Alpha rule." in _every("sess-2")
        assert _HOST_WIDE not in _every("sess-2") + _due("sess-2")

    def test_nobody_gets_it_while_nobody_owns_the_slot(self, published: None, registry: Path) -> None:
        registry.unlink()

        assert "Alpha rule." in _every()
        assert _HOST_WIDE not in _every()

    def test_an_owner_whose_process_is_gone_owns_nothing(self, published: None, registry: Path) -> None:
        _own_the_loop(registry, "sess-1", pid=2**22 + 12345)

        assert _HOST_WIDE not in _every()

    def test_the_slot_is_read_never_claimed(self, published: None, registry: Path, state_dir: Path) -> None:
        registry.unlink()
        (state_dir / "sess-2.teatree-active").touch()

        _every("sess-2")
        _due("sess-2")

        assert not registry.exists()

    def test_the_compiled_pr_board_is_host_wide(self, engaged: None, state_dir: Path) -> None:
        (state_dir / "sess-2.teatree-active").touch()
        board = next(d.text for d in compiled_directives() if d.slot_id == "standing-pr-board")

        assert board in _every()
        assert board not in _every("sess-2")


# ── nothing is recorded until it was written ─────────────────────────


class TestTheRecordFollowsTheWrite:
    def test_rules_not_marked_delivered_come_back(self, published: None, state_dir: Path) -> None:
        unwritten = due_rules("sess-1", every_slot=False)

        assert "Alpha rule." in unwritten.text
        assert not (state_dir / "sess-1.directives-injected").exists()
        assert "Alpha rule." in _due()

    def test_marking_records_exactly_the_slots_handed_out(self, published: None, state_dir: Path) -> None:
        _due()
        _backdate(state_dir, "slot-b", 90)
        before = json.loads((state_dir / "sess-1.directives-injected").read_text(encoding="utf-8"))

        _due()

        after = json.loads((state_dir / "sess-1.directives-injected").read_text(encoding="utf-8"))
        assert after["slot-a"] == before["slot-a"]
        assert after["slot-b"] > before["slot-b"]


# ── the cadence ──────────────────────────────────────────────────────


class TestTheCadence:
    """Re-delivery, not emit-once, is the mechanism — throttled to each slot's own interval."""

    def test_the_first_delivery_carries_every_slot_and_records_the_instant(
        self, published: None, state_dir: Path
    ) -> None:
        before = time.time()

        rendered = _due()

        assert rendered.startswith("Standing rules for this session (3)")
        stamps = json.loads((state_dir / "sess-1.directives-injected").read_text(encoding="utf-8"))
        assert set(stamps) == {"slot-a", "slot-b", "slot-c"}
        assert all(stamp >= before for stamp in stamps.values())

    def test_a_second_look_inside_every_interval_stays_silent(self, published: None) -> None:
        _due()

        assert _due() == ""

    def test_only_the_slot_whose_interval_passed_comes_back(self, published: None, state_dir: Path) -> None:
        _due()
        _backdate(state_dir, "slot-b", 90)

        rendered = _due()

        assert "Beta rule." in rendered
        assert "Alpha rule." not in rendered

    def test_session_start_delivers_every_slot_however_recent(self, published: None) -> None:
        _due()

        assert "Alpha rule." in _every()
        assert "Beta rule." in _every()

    def test_session_start_restarts_every_interval(self, published: None) -> None:
        _every()

        assert _due() == ""

    def test_each_session_keeps_its_own_cadence(self, published: None, state_dir: Path) -> None:
        (state_dir / "sess-2.teatree-active").touch()
        _due("sess-1")

        assert "Alpha rule." in _due("sess-2")
        assert _due("sess-1") == ""


# ── what is delivered ────────────────────────────────────────────────


class TestTheSource:
    def test_the_publication_wins_over_the_compiled_defaults(self, published: None) -> None:
        rendered = _due()

        assert "Alpha rule." in rendered
        assert all(text not in rendered for text in _DEFAULT_TEXTS)

    def test_nothing_published_yet_delivers_the_compiled_defaults(self, engaged: None) -> None:
        rendered = _due()

        assert all(directive.text in rendered for directive in compiled_directives())

    def test_a_corrupt_publication_delivers_the_compiled_defaults(self, engaged: None) -> None:
        cache = standing_directives_cache.cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{ not json", encoding="utf-8")

        rendered = _due()

        assert all(directive.text in rendered for directive in compiled_directives())

    def test_every_slot_switched_off_delivers_nothing(self, engaged: None) -> None:
        standing_directives_cache.write([])

        assert _every() == ""


# ── fail-open ────────────────────────────────────────────────────────


class TestFailOpen:
    def test_an_unwritable_state_dir_still_delivers(
        self, published: None, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocked = state_dir / "sess-1.teatree-active"
        monkeypatch.setenv("T3_HOOK_STATE_DIR", str(blocked))
        monkeypatch.setenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", str(blocked))
        monkeypatch.setenv("T3_AUTOLOAD", "1")

        assert "Alpha rule." in _due()

    def test_an_unreadable_marker_delivers_every_slot(self, published: None, state_dir: Path) -> None:
        (state_dir / "sess-1.directives-injected").write_text("{ not json", encoding="utf-8")

        assert "Beta rule." in _due()

    def test_no_staging_file_is_left_in_the_state_dir(self, published: None, state_dir: Path) -> None:
        _due()
        _every()

        assert sorted(path.name for path in state_dir.iterdir()) == [
            "sess-1.directives-injected",
            "sess-1.teatree-active",
        ]
