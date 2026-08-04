# test-path: cross-cutting — tests hooks/scripts/loop_registrations.py (hooks/); no src/teatree/ mirror.
"""The Claude-plugin adapter that delivers the standing directives (#4166 Phase 1).

Layer 2 of the harness-neutrality split: it renders whatever the harness-neutral
resolver returns as ``/loop <duration> [<slot_id>] <text>`` registrations and
delivers them to every ``_teatree_engaged`` session (the SDK lane excluded). It
carries NO directive text, NO cadence value, and no policy — the layer-purity
test below pins that by round-tripping a fake resolver's text verbatim.
"""

import io
from pathlib import Path
from unittest import mock

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import loop_registrations
from hooks.scripts.loop_registrations import emit_standing_directives, emit_standing_directives_once

_FAKE = [
    {"slot_id": "slot-a", "cadence_seconds": 300, "text": "Alpha rule.", "scope": "attended"},
    {"slot_id": "slot-b", "cadence_seconds": 90, "text": "Beta rule.", "scope": "attended"},
]


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(router, "STATE_DIR", tmp_path)
    return tmp_path


class TestEmitStandingDirectives:
    def test_renders_one_loop_registration_per_resolved_directive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        out = io.StringIO()

        assert emit_standing_directives(out) is True
        text = out.getvalue()

        assert "/loop 5m [slot-a] Alpha rule." in text
        assert "/loop 90s [slot-b] Beta rule." in text

    def test_the_resolved_text_round_trips_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Layer purity: the adapter owns rendering, never content. A resolver
        # text it has never seen must reach the stream unaltered.
        novel = "A directive the adapter cannot possibly know about."
        monkeypatch.setattr(
            loop_registrations,
            "_standing_directives",
            lambda: [{"slot_id": "slot-x", "cadence_seconds": 600, "text": novel, "scope": "attended"}],
        )
        out = io.StringIO()
        emit_standing_directives(out)

        assert novel in out.getvalue()

    def test_nothing_resolvable_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", list)
        out = io.StringIO()

        assert emit_standing_directives(out) is False
        assert out.getvalue() == ""

    def test_a_raising_resolver_fails_open_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> list[dict[str, object]]:
            msg = "resolver exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(loop_registrations, "_standing_directives", boom)
        out = io.StringIO()

        assert emit_standing_directives(out) is False
        assert out.getvalue() == ""

    def test_carries_no_directive_text_or_cadence_literal_of_its_own(self) -> None:
        source = Path(loop_registrations.__file__).read_text(encoding="utf-8")

        for policy_word in ("PlanArtifact", "skip-planning", "ticket clear", "maker ≠ checker"):
            assert policy_word not in source
        for cadence in ("300", "1800", "600"):
            assert cadence not in source


class TestEmitStandingDirectivesOnce:
    def test_an_engaged_session_gets_them_and_the_marker_is_written(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: True)
        out = io.StringIO()

        assert emit_standing_directives_once("sess-1", out) is True
        assert "Alpha rule." in out.getvalue()
        assert (state_dir / "sess-1.directives-pending").is_file()

    def test_the_second_prompt_in_the_same_session_is_silent(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: True)
        emit_standing_directives_once("sess-1", io.StringIO())

        out = io.StringIO()
        assert emit_standing_directives_once("sess-1", out) is False
        assert out.getvalue() == ""

    def test_an_unengaged_session_gets_nothing(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: False)
        out = io.StringIO()

        assert emit_standing_directives_once("sess-1", out) is False
        assert out.getvalue() == ""
        assert not (state_dir / "sess-1.directives-pending").exists()

    def test_the_sdk_lane_is_excluded(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Factory workers are FSM-governed and have no user-request channel, so
        # the session-scoped directives are for the attended lane only.
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: True)
        monkeypatch.setenv("CLAUDE_AGENT_SDK_VERSION", "0.1.0")
        out = io.StringIO()

        assert emit_standing_directives_once("sess-1", out) is False
        assert out.getvalue() == ""

    def test_an_empty_session_id_is_a_no_op(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        out = io.StringIO()

        assert emit_standing_directives_once("", out) is False
        assert out.getvalue() == ""

    def test_a_raising_engagement_probe_fails_open_silently(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", mock.Mock(side_effect=OSError("boom")))
        out = io.StringIO()

        assert emit_standing_directives_once("sess-1", out) is False
        assert out.getvalue() == ""


class TestRouterDelivery:
    """The UserPromptSubmit handler delivers them BEFORE the loop-owner election."""

    def test_a_non_owner_engaged_session_still_gets_the_standing_directives(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reactive infra slots are singleton infrastructure (owner-only); the
        # standing directives are per-session behaviour, so a loser of the
        # owner election must still receive them.
        monkeypatch.setattr(loop_registrations, "_standing_directives", lambda: _FAKE)
        monkeypatch.setattr(router, "_teatree_engaged", lambda _sid: True)
        monkeypatch.setattr(router, "_loop_auto_load_active", lambda _sid: False)
        emitted = io.StringIO()

        with mock.patch("sys.stdout", emitted):
            router.handle_enforce_loop_on_prompt({"session_id": "sess-1"})

        assert "Alpha rule." in emitted.getvalue()
