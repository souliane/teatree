# test-path: cross-cutting — a hooks/scripts hook module; no src/teatree/ mirror.
"""The SubagentStop arm that hands an admission seat back (#4129).

An admitted interactive dispatch takes a durable seat at the ``PreToolUse`` gate.
Without this arm the seat could only lapse on its window, so the ceiling would bound a
dispatch RATE rather than the live population it is written to bound.
"""

import pytest

import hooks.scripts.dispatch_seat_release as release_arm
import hooks.scripts.django_bootstrap as bootstrap
import hooks.scripts.hook_router as router
from teatree.core import dispatch_admission as core

_EXPLODED = "release exploded"
_MUST_NOT_RELEASE = "no seat should have been released"


def _exploding_release(*, session_id: str, agent_id: str) -> bool:
    raise RuntimeError(_EXPLODED)


def _must_not_release(*, session_id: str, agent_id: str) -> bool:
    raise AssertionError(_MUST_NOT_RELEASE)


def _stop(**extra: object) -> dict:
    return {"session_id": "sess-4129", "agent_id": "a-1", **extra}


class TestSeatRelease:
    def test_it_releases_the_terminating_agents_seat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        released: list[tuple[str, str]] = []
        monkeypatch.setattr(
            core,
            "release_interactive_dispatch",
            lambda *, session_id, agent_id: bool(released.append((session_id, agent_id))) or True,
        )
        release_arm.handle_subagent_stop_release(_stop())
        assert released == [("sess-4129", "a-1")]

    def test_the_main_agents_own_stop_releases_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Stop with no agent_id is the orchestrator itself — it holds no seat.
        monkeypatch.setattr(core, "release_interactive_dispatch", _must_not_release)
        release_arm.handle_subagent_stop_release({"session_id": "sess-4129"})
        release_arm.handle_subagent_stop_release({"agent_id": "a-1"})

    def test_an_unbootstrappable_django_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bootstrap, "bootstrap_teatree_django", lambda: False)
        monkeypatch.setattr(core, "release_interactive_dispatch", _must_not_release)
        release_arm.handle_subagent_stop_release(_stop())

    def test_an_internal_error_never_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(core, "release_interactive_dispatch", _exploding_release)
        release_arm.handle_subagent_stop_release(_stop())


class TestRegistration:
    def test_the_arm_is_registered_on_subagent_stop(self) -> None:
        assert release_arm.handle_subagent_stop_release in router._HANDLERS["SubagentStop"]

    def test_the_router_reexports_the_same_object(self) -> None:
        assert router.handle_subagent_stop_release is release_arm.handle_subagent_stop_release
