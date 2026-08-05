# test-path: cross-cutting — tests hooks/scripts/loop_registrations.py (hooks/); no src/teatree/ mirror.
"""The Claude-plugin adapter that delivers the standing directives (#4166 Phase 1).

Layer 2 of the harness-neutrality split. It maps each resolved directive's
declared delivery cost onto this harness's own mechanisms — a zero-turn slot is
written as context into the turn already happening, a self-waking slot becomes a
recurring ``/loop`` registration — and it carries NO directive text, NO cadence
value and no policy of its own.

Three of this module's guards used to pass vacuously, so each of the three below
ships with a CONTROL corpus: violating inputs asserted CAUGHT and legitimate ones
asserted CLEAN, so every green here is one whose red has been observed.
"""

import io
import operator
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from unittest import mock

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import loop_registrations
from hooks.scripts.loop_registrations import emit_standing_directives_once
from teatree.loop.standing_directives import MAX_DIRECTIVE_CHARS, STANDING_DIRECTIVES

_FAKE = [
    {"slot_id": "slot-a", "cadence_seconds": 300, "text": "Alpha rule.", "scope": "attended", "wakes_session": False},
    {"slot_id": "slot-b", "cadence_seconds": 90, "text": "Beta rule.", "scope": "attended", "wakes_session": True},
    {
        "slot_id": "slot-c",
        "cadence_seconds": 600,
        "text": "Gamma rule.",
        "scope": "attended-singleton",
        "wakes_session": True,
    },
]

_ADAPTER_SOURCE = Path(loop_registrations.__file__).read_text(encoding="utf-8")
_DEFAULT_TEXTS = [d.default_text for d in STANDING_DIRECTIVES]


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(router, "STATE_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def not_the_sdk_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the SDK-lane seam the ambient env opens.

    A headless factory agent runs the suite with ``CLAUDE_AGENT_SDK_VERSION`` /
    ``CLAUDE_CODE_ENTRYPOINT=sdk-py`` exported, which is exactly the lane the
    adapter excludes — so an unpinned env decides the assertion, not the code.
    """
    monkeypatch.delenv("CLAUDE_AGENT_SDK_VERSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


@pytest.fixture
def engaged(monkeypatch: pytest.MonkeyPatch, not_the_sdk_lane: None) -> None:
    """Every gate open: engaged, loop-arming allowed, and this session owns the tick."""
    monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
    monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: True)
    monkeypatch.setattr(router, "_loop_auto_load_active", lambda _sid: True)
    monkeypatch.setattr(router, "_claim_loop_ownership", lambda _sid: None)
    monkeypatch.setattr(router, "_session_owns_loop", lambda _sid: True)


def _emit(session_id: str = "sess-1") -> str:
    out = io.StringIO()
    emit_standing_directives_once(session_id, out)
    return out.getvalue()


def _loop_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "/loop " in line]


_TWO_SESSIONS = ("sess-1", "sess-2")
_ALTERNATING_ROUNDS = 5

# The retention window scaled from two days down to seconds, so a session can be
# driven ACROSS it under a single real clock. Faking the clock is not available:
# the sweep compares its own ``now`` against real filesystem mtimes, so a fake
# ``now`` measures nothing. The prompt interval keeps ~7x headroom against the
# window, so only a multi-second scheduling stall could age a refreshed marker out.
_PROBE_WINDOW_SECONDS = 2.0
_PROBE_PROMPT_INTERVAL = 0.3
_PROBE_PROMPTS = 14


def _age_out(marker: Path) -> None:
    """Backdate *marker* past the router's retention window and re-arm the sweep throttle."""
    stale = time.time() - router._STATE_FILE_MAX_AGE_SECONDS - 1
    os.utime(marker, (stale, stale))
    (marker.parent / router._SWEEP_SENTINEL).unlink(missing_ok=True)


# ── minor 6: the policy-leak check, derived from layer 1 ─────────────


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 3}


#: Delivery vocabulary the adapter's own prose legitimately shares with the
#: layer-1 texts: the mechanism's nouns (a session, a request, a marker's data),
#: the fail-open contract's words, and ordinary connectives. Nothing here names
#: what a directive SAYS. Widening this set is a deliberate review decision —
#: that is the whole point of deriving the denylist rather than hand-picking it.
_ADAPTER_PLUMBING = frozenset(
    {
        "cannot",
        "cold",
        "every",
        "first",
        "from",
        "list",
        "must",
        "never",
        "none",
        "only",
        "open",
        "request",
        "safe",
        "session",
        "standing",
        "state",
        "test",
        "then",
        "this",
        "turn",
        "user",
        "with",
        "work",
    }
)


def policy_leaks(source: str, texts: Iterable[str]) -> list[str]:
    """Distinctive layer-1 policy words that appear in *source* — empty means pure."""
    distinctive = {word for text in texts for word in _words(text)} - _ADAPTER_PLUMBING
    return sorted(distinctive & _words(source))


_LEAKING_SOURCES = {
    # The reviewer's paraphrase: it is not a verbatim copy, so the shipped
    # four-token grep passed it.
    "paraphrased_header": 'stream.write("Golden rule: PLAN then IMPLEMENT then COLD REVIEW\\n")',
    "verbatim_slice": '    """Fresh merge_safe at the LIVE head with green CI → merge now via the keystone."""',
    "bare_comment": "    # maker ≠ checker, so never dispatch the reviewer that wrote the branch",
}


# ── minor 7: the exact-equality round trip and its mutant corpus ─────

_SENTINEL_SUFFIX = "END-OF-DIRECTIVE"


def _probe_text(slot_id: str) -> str:
    """A directive at the cap, ending in a sentinel, so any truncation loses the tail."""
    sentinel = f"[{slot_id}-{_SENTINEL_SUFFIX}]."
    filler = ("Probe body for the exact-equality round trip. " * 40)[: MAX_DIRECTIVE_CHARS - len(sentinel) - 1]
    return f"{filler} {sentinel}"


_PROBES = [
    {
        "slot_id": "probe-context",
        "cadence_seconds": 300,
        "text": _probe_text("probe-context"),
        "scope": "attended",
        "wakes_session": False,
    },
    {
        "slot_id": "probe-waking",
        "cadence_seconds": 600,
        "text": _probe_text("probe-waking"),
        "scope": "attended",
        "wakes_session": True,
    },
    {
        "slot_id": "probe-singleton",
        "cadence_seconds": 900,
        "text": _probe_text("probe-singleton"),
        "scope": "attended-singleton",
        "wakes_session": True,
    },
]

_EXPECTED_PROBE_LINES = [
    f"  - [probe-context] {_PROBES[0]['text']}",
    f"  - /loop 10m [probe-waking] {_PROBES[1]['text']}",
    f"  - /loop 15m [probe-singleton] {_PROBES[2]['text']}",
]


_ROUND_TRIP_MUTANTS: dict[str, Callable[[str], str]] = {
    "truncate_200": operator.itemgetter(slice(200)),
    "truncate_1000": operator.itemgetter(slice(1000)),
    "strip_trailing_period": lambda rendered: "\n".join(line.rstrip(".") for line in rendered.split("\n")),
    "collapse_whitespace": lambda rendered: " ".join(rendered.split()),
    "upper": str.upper,
}


def _assert_round_trip(render: Callable[[str, io.StringIO], object]) -> None:
    """Every resolved directive reaches the stream as EXACTLY its rendered line."""
    out = io.StringIO()
    render("sess-round-trip", out)
    emitted = [line for line in out.getvalue().split("\n") if line.startswith("  - ")]

    assert emitted == _EXPECTED_PROBE_LINES


def _mutating_render(mutate: Callable[[str], str]) -> Callable[[str, io.StringIO], object]:
    def render(session_id: str, stream: io.StringIO) -> object:
        buffered = io.StringIO()
        result = emit_standing_directives_once(session_id, buffered)
        stream.write(mutate(buffered.getvalue()))
        return result

    return render


class TestLayerPurity:
    """Minor 6: a NEW policy word must be caught by default, not by hand-listing it."""

    def test_the_real_adapter_carries_no_layer_one_policy_word(self) -> None:
        assert policy_leaks(_ADAPTER_SOURCE, _DEFAULT_TEXTS) == []

    @pytest.mark.parametrize("fake_source", _LEAKING_SOURCES.values(), ids=list(_LEAKING_SOURCES))
    def test_control_a_leaking_source_is_caught(self, fake_source: str) -> None:
        # CONTROL — the shipped four-token grep passed all three of these.
        assert policy_leaks(fake_source, _DEFAULT_TEXTS) != []

    def test_carries_no_cadence_literal_of_its_own(self) -> None:
        # Kept verbatim: this half of the guard is strict and caught its mutation.
        for cadence in ("300", "1800", "600"):
            assert cadence not in _ADAPTER_SOURCE


class TestTheVerbatimRoundTrip:
    """Minor 7: exact line equality over every resolved directive, at the cap."""

    @pytest.fixture(autouse=True)
    def _probe_resolver(self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _PROBES)

    def test_every_resolved_directive_round_trips_exactly(self) -> None:
        _assert_round_trip(emit_standing_directives_once)

    @pytest.mark.parametrize("mutant", _ROUND_TRIP_MUTANTS.values(), ids=list(_ROUND_TRIP_MUTANTS))
    def test_control_a_mutated_render_fails_the_round_trip(self, mutant: Callable[[str], str]) -> None:
        # CONTROL — a `text[:200]` adapter mutation truncates all three real
        # directives, and the shipped 51-character containment probe passed it.
        with pytest.raises(AssertionError):
            _assert_round_trip(_mutating_render(mutant))


class TestTheGateMatrix:
    """Which session gets which delivery shape — the #256 contract, per slot."""

    def test_engaged_but_not_loop_arming_gets_context_and_zero_registrations(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reviewer's reproduction, as a permanent regression test: a session
        # engaged by a lifecycle skill alone must arm NOTHING.
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: True)
        monkeypatch.setattr(router, "_loop_auto_load_active", lambda _sid: False)

        emitted = _emit()

        assert "Alpha rule." in emitted
        assert _loop_lines(emitted) == []

    def test_loop_arming_but_not_the_owner_skips_only_the_singleton_slot(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_session_owns_loop", lambda _sid: False)

        emitted = _emit()

        assert "Alpha rule." in emitted
        assert "[slot-b]" in emitted
        assert "[slot-c]" not in emitted

    def test_the_owner_gets_every_shape(self, state_dir: Path, engaged: None) -> None:
        emitted = _emit()

        assert "  - [slot-a] Alpha rule." in emitted
        assert "  - /loop 90s [slot-b] Beta rule." in emitted
        assert "  - /loop 10m [slot-c] Gamma rule." in emitted

    def test_an_unengaged_session_gets_nothing(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: False)
        monkeypatch.setattr(router, "_loop_auto_load_active", lambda _sid: False)

        assert _emit() == ""

    def test_an_unengaged_session_never_reaches_the_resolver(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The resolver bootstraps Django (~1.7s cold) on the hot UserPromptSubmit
        # path, so a session that can emit nothing must not reach it (#22).
        resolver = mock.Mock(return_value=_FAKE)
        monkeypatch.setattr(loop_registrations, "_standing_directives", resolver)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: False)
        monkeypatch.setattr(router, "_loop_auto_load_active", lambda _sid: False)

        assert _emit() == ""
        resolver.assert_not_called()

    def test_a_throttled_prompt_never_reaches_the_resolver(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same cost, paid per prompt: an engaged session that arms no loop has
        # nothing but the cadence-throttled context slots to deliver.
        monkeypatch.setattr(router, "_loop_auto_load_active", lambda _sid: False)
        assert "Alpha rule." in _emit()
        resolver = mock.Mock(return_value=_FAKE)
        monkeypatch.setattr(loop_registrations, "_standing_directives", resolver)

        assert _emit() == ""
        resolver.assert_not_called()

    def test_the_sdk_lane_is_excluded(self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch) -> None:
        # Factory workers are FSM-governed and have no user-request channel.
        monkeypatch.setenv("CLAUDE_AGENT_SDK_VERSION", "0.1.0")

        assert _emit() == ""

    def test_an_empty_session_id_is_a_no_op(self, state_dir: Path, engaged: None) -> None:
        assert _emit("") == ""

    def test_nothing_resolvable_is_silent(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", list)

        assert _emit() == ""


class TestTheInjectionThrottle:
    """The zero-turn shape is re-delivered, not emitted once — that IS the mechanism."""

    def test_the_first_prompt_injects_and_records_the_instant(self, state_dir: Path, engaged: None) -> None:
        assert "Alpha rule." in _emit()
        assert (state_dir / "sess-1.directives-injected").is_file()

    def test_a_second_prompt_inside_the_interval_stays_silent(self, state_dir: Path, engaged: None) -> None:
        _emit()

        assert "Alpha rule." not in _emit()

    def test_a_prompt_after_the_interval_injects_again(self, state_dir: Path, engaged: None) -> None:
        _emit()
        marker = state_dir / "sess-1.directives-injected"
        marker.write_text('{"slot-a": 1.0}', encoding="utf-8")

        assert "Alpha rule." in _emit()

    def test_the_waking_slots_are_registered_once_per_session(self, state_dir: Path, engaged: None) -> None:
        _emit()

        assert _loop_lines(_emit()) == []

    def test_an_unwritable_state_dir_is_silent_and_still_delivers(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_ensure_state_dir", mock.Mock(side_effect=OSError("read-only")))

        assert "Alpha rule." in _emit()

    def test_a_dead_sessions_markers_age_out(self, state_dir: Path, engaged: None) -> None:
        dead = state_dir / "sess-old.directives-injected"
        dead.write_text("{}", encoding="utf-8")
        _age_out(dead)

        _emit()

        assert not dead.exists()

    def test_control_the_marker_survives_when_the_routers_sweep_is_off(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CONTROL — the reaping above is the router's age sweep and nothing else.
        # With only the sweep disabled, the very same backdated marker survives.
        monkeypatch.setattr(router, "_sweep_stale_state_files", lambda: None)
        dead = state_dir / "sess-old.directives-injected"
        dead.write_text("{}", encoding="utf-8")
        _age_out(dead)

        _emit()

        assert dead.exists()


class TestASessionOutlivingTheRetentionWindow:
    """A live session may not age its OWN registrations out and re-register them.

    ``directives-registered`` is written once, so its mtime would track the
    registration instant rather than the session: the router's age sweep would reap
    a running session's marker and every ``/loop`` slot would be registered afresh,
    once per retention window, for as long as the session lives — the same unbounded
    registration growth ``self_woken_turns_per_hour`` exists to bound, at a two-day
    period instead of a per-prompt one. The rewrite-unchanged is what makes age a
    true liveness signal for this marker, and only a run ACROSS the window shows it.

    Each test carries its own control for the way this probe can lie — a sweep that
    never fires reports the same green as a marker that correctly survived it.
    """

    @pytest.fixture(autouse=True)
    def _scaled_window(self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "_STATE_FILE_MAX_AGE_SECONDS", _PROBE_WINDOW_SECONDS)
        monkeypatch.setattr(router, "_SWEEP_THROTTLE_SECONDS", 0)

    def test_no_slot_is_re_registered_while_the_session_keeps_prompting(self, state_dir: Path) -> None:
        # CONTROL, in-test: a marker of the SAME suffix written once and never
        # rewritten — the mutant's behaviour — must be reaped by the end of the run.
        # It fails with the live marker if no sweep ran, and alone if one did.
        frozen = state_dir / "sess-frozen.directives-registered"
        frozen.write_text("{}", encoding="utf-8")
        started = time.time()

        registered = []
        for _ in range(_PROBE_PROMPTS):
            router._ensure_state_dir()  # the sweep every other hook's state write fires
            registered.append(len(_loop_lines(_emit())))
            time.sleep(_PROBE_PROMPT_INTERVAL)

        assert time.time() - started > 2 * _PROBE_WINDOW_SECONDS
        assert not frozen.exists()
        assert (state_dir / "sess-1.directives-registered").is_file()
        assert registered == [2, *([0] * (_PROBE_PROMPTS - 1))]

    def test_a_prompt_whose_waking_slots_are_all_braked_still_refreshes_the_marker(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The merged-mode brake drops every waking slot, so the prompt has no
        # candidates at all. Returning early on that leaves the marker untouched,
        # so a session held away past the window re-registers on its return.
        _emit()
        marker = state_dir / "sess-1.directives-registered"
        _age_out(marker)
        monkeypatch.setattr(
            loop_registrations, "_standing_directives", lambda: [d for d in _FAKE if not d["wakes_session"]]
        )

        _emit()
        router._sweep_stale_state_files()

        assert marker.is_file()

    def test_control_an_unrefreshed_marker_is_reaped_by_that_same_sweep(self, state_dir: Path) -> None:
        # CONTROL for the test above — the sweep it ends on genuinely reaps a
        # marker of that suffix at that age, so the survival there is the rewrite.
        _emit()
        marker = state_dir / "sess-1.directives-registered"
        _age_out(marker)

        router._sweep_stale_state_files()

        assert not marker.exists()


class TestConcurrentLiveSessions:
    """N>=2 engaged sessions is the normal operating mode, so one prompt may not undo another's.

    Every assertion here is on a SECOND live session's state, which the
    single-session throttle tests above cannot reach: they hold for a solo
    session whether or not a prompt destroys everyone else's markers.
    """

    def test_each_sessions_markers_survive_the_others_prompts(self, state_dir: Path, engaged: None) -> None:
        for _ in range(_ALTERNATING_ROUNDS):
            _emit("sess-1")
            _emit("sess-2")

        assert sorted(p.name for p in state_dir.glob("*.directives-*")) == [
            "sess-1.directives-injected",
            "sess-1.directives-registered",
            "sess-2.directives-injected",
            "sess-2.directives-registered",
        ]

    def test_the_waking_slots_are_registered_once_per_session(self, state_dir: Path, engaged: None) -> None:
        registered = [len(_loop_lines(_emit(sid))) for _ in range(_ALTERNATING_ROUNDS) for sid in _TWO_SESSIONS]

        assert registered == [2, 2, *([0] * 8)]

    def test_the_zero_turn_rule_is_injected_once_per_cadence_not_once_per_prompt(
        self, state_dir: Path, engaged: None
    ) -> None:
        injected = [_emit(sid).count("Alpha rule.") for _ in range(_ALTERNATING_ROUNDS) for sid in _TWO_SESSIONS]

        assert injected == [1, 1, *([0] * 8)]


class TestFailOpen:
    """Every probe this adapter consults may fail; none of them may raise into the hook."""

    def test_a_raising_resolver_writes_nothing(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", mock.Mock(side_effect=RuntimeError("boom")))

        assert _emit() == ""

    def test_a_raising_engagement_probe_writes_nothing(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_teatree_engaged", mock.Mock(side_effect=OSError("boom")))

        assert _emit() == ""

    def test_a_raising_ownership_probe_arms_nothing(
        self, state_dir: Path, engaged: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(router, "_session_owns_loop", mock.Mock(side_effect=OSError("boom")))

        assert _loop_lines(_emit()) == []


class TestRouterDelivery:
    """The UserPromptSubmit handler delivers them BEFORE the loop-owner election."""

    def test_a_session_that_never_armed_the_loop_still_gets_the_zero_turn_rule(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: True)
        monkeypatch.setattr(router, "_loop_auto_load_active", lambda _sid: False)
        emitted = io.StringIO()

        with mock.patch("sys.stdout", emitted):
            router.handle_enforce_loop_on_prompt({"session_id": "sess-1"})

        assert "Alpha rule." in emitted.getvalue()
        assert _loop_lines(emitted.getvalue()) == []
