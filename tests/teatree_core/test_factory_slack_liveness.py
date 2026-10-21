"""``factory_slack_live``: Slack is live only while heartbeat, owner transport and the t3-master lease all hold."""

import json
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import LoopLease
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from teatree.core.slack_first.factory_slack_liveness import factory_slack_live


@pytest.fixture(autouse=True)
def _database(db: None) -> None:
    """Every test here reads the ``t3-master`` lease from the database."""


_OVERLAY = "t3-slack"
_OWNER_ID = "UOWNER"


@dataclass(frozen=True)
class _Transport:
    user_id: str = _OWNER_ID


@contextmanager
def _transports(overlays: dict[str, object | None]) -> Generator[None]:
    with (
        patch("teatree.core.backend_factory.OverlayConfigResolver.all_names", return_value=sorted(overlays)),
        patch(
            "teatree.core.backend_factory.configured_messaging_from_overlay",
            side_effect=lambda name=None: overlays.get(name or ""),
        ),
    ):
        yield


@pytest.fixture
def heartbeat_dir(tmp_path: Path) -> Iterator[Path]:
    with patch("teatree.core.slack_first.slack_listener_heartbeat.DATA_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def held_lease() -> Iterator[None]:
    LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID, owner_pid=None)
    yield
    LoopLease.objects.release_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID)


def _stamp(directory: Path, *, age_seconds: float = 0.0, overlay: str = _OVERLAY) -> None:
    at = time.time() - age_seconds
    payload = {"updated_at": int(at), "interval_seconds": 15, "overlays": {overlay: at}}
    (directory / "slack-listener-heartbeat.json").write_text(json.dumps(payload), encoding="utf-8")


def test_fresh_heartbeat_resolved_backend_and_held_lease_is_live(heartbeat_dir: Path, held_lease: None) -> None:
    _stamp(heartbeat_dir)
    with _transports({_OVERLAY: _Transport()}):
        verdict = factory_slack_live(_OVERLAY)

    assert verdict.live
    assert verdict.reason == ""
    assert verdict.overlay == _OVERLAY
    assert verdict.owner_id == _OWNER_ID


def test_absent_heartbeat_is_not_live(heartbeat_dir: Path, held_lease: None) -> None:
    with _transports({_OVERLAY: _Transport()}):
        verdict = factory_slack_live(_OVERLAY)

    assert not verdict.live
    assert "heartbeat_absent" in verdict.reason


@pytest.mark.parametrize(
    "contents",
    ["{not json", json.dumps({"interval_seconds": 15, "overlays": {_OVERLAY: 1.0}})],
    ids=["invalid-json", "no-updated-at"],
)
def test_unparsable_heartbeat_is_not_live(heartbeat_dir: Path, held_lease: None, contents: str) -> None:
    (heartbeat_dir / "slack-listener-heartbeat.json").write_text(contents, encoding="utf-8")
    with _transports({_OVERLAY: _Transport()}):
        verdict = factory_slack_live(_OVERLAY)

    assert not verdict.live
    assert verdict.reason.startswith("heartbeat_absent")


def test_stale_heartbeat_is_not_live(heartbeat_dir: Path, held_lease: None) -> None:
    _stamp(heartbeat_dir, age_seconds=3600)
    with _transports({_OVERLAY: _Transport()}):
        verdict = factory_slack_live(_OVERLAY)

    assert not verdict.live
    assert "stale" in verdict.reason


def test_unresolved_messaging_backend_is_not_live(heartbeat_dir: Path, held_lease: None) -> None:
    _stamp(heartbeat_dir)
    with _transports({_OVERLAY: None}):
        verdict = factory_slack_live(_OVERLAY)

    assert not verdict.live
    assert "messaging" in verdict.reason


def test_empty_owner_id_is_not_live(heartbeat_dir: Path, held_lease: None) -> None:
    _stamp(heartbeat_dir)
    with _transports({_OVERLAY: _Transport(user_id="")}):
        verdict = factory_slack_live(_OVERLAY)

    assert not verdict.live
    assert "owner" in verdict.reason


def test_unheld_master_lease_is_not_live(heartbeat_dir: Path) -> None:
    _stamp(heartbeat_dir)
    with _transports({_OVERLAY: _Transport()}):
        verdict = factory_slack_live(_OVERLAY)

    assert not verdict.live
    assert "t3-master" in verdict.reason


def test_a_caller_overlay_without_a_transport_is_judged_on_the_credentialed_overlay(
    heartbeat_dir: Path, held_lease: None
) -> None:
    _stamp(heartbeat_dir)
    with _transports({"t3-noop": None, _OVERLAY: _Transport()}):
        verdict = factory_slack_live("t3-noop")

    assert verdict.live
    assert verdict.overlay == _OVERLAY


def test_two_credentialed_overlays_with_no_preference_are_ambiguous_and_not_live(
    heartbeat_dir: Path, held_lease: None
) -> None:
    _stamp(heartbeat_dir)
    with _transports({"t3-a": _Transport(), "t3-b": _Transport()}):
        verdict = factory_slack_live("t3-noop")

    assert not verdict.live
    assert "ambiguous" in verdict.reason


def test_a_failing_leg_is_not_live_and_names_the_exception(heartbeat_dir: Path, held_lease: None) -> None:
    _stamp(heartbeat_dir)
    with (
        _transports({_OVERLAY: _Transport()}),
        patch(
            "teatree.core.slack_first.factory_slack_liveness.t3_master_lease_held", side_effect=RuntimeError("db down")
        ),
    ):
        verdict = factory_slack_live(_OVERLAY)

    assert not verdict.live
    assert "RuntimeError" in verdict.reason
