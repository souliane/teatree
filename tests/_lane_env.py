"""Shared test-infra helper: state a session lane through its env seam (#3973).

``session_lane`` reads three env markers, and a factory / Agent-SDK runner exports
them — so a fixture that MERGES into ``os.environ`` inherits the runner's lane
instead of stating one. Under the headless authoring gate, which fails OPEN for
every lane but a positively-identified interactive CLI, that inheritance turns each
refuse-case green by ALLOWING: the suite reports success while asserting nothing,
and only CI — where no marker exists — held the gate honest.

Clearing every marker before setting the lane's own is what makes a pin a statement
rather than an overlay on whatever the runner left behind, and the ``session_lane()``
assertion is the control: without it a mis-stated env is indistinguishable from a
correct one, since both run and neither errors.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

import pytest

from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI, LANE_SDK, LANE_UNKNOWN, session_lane

ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"
INTERACTIVE_ENV = "CLAUDECODE"
SDK_VERSION_ENV = "CLAUDE_AGENT_SDK_VERSION"

#: Every marker :func:`session_lane` reads. A pin that clears a subset leaves the
#: runner's own value deciding the lane through whichever key it forgot.
LANE_KEYS: tuple[str, ...] = (ENTRYPOINT_ENV, INTERACTIVE_ENV, SDK_VERSION_ENV)

_LANE_ENV: Mapping[str, Mapping[str, str]] = {
    LANE_INTERACTIVE_CLI: {ENTRYPOINT_ENV: "cli", INTERACTIVE_ENV: "1"},
    LANE_SDK: {ENTRYPOINT_ENV: "sdk-py", SDK_VERSION_ENV: "0.2.95"},
    LANE_UNKNOWN: {},
}


@contextmanager
def _restored(keys: tuple[str, ...]) -> Iterator[None]:
    """Restore *keys* to their entry values on exit, deleting the ones that were absent."""
    saved = {key: os.environ.get(key) for key in keys}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def ambient_lane_env_stripped() -> Iterator[None]:
    """Every lane marker removed for the block — an unstated lane reads UNKNOWN, as in CI."""
    with _restored(LANE_KEYS):
        for key in LANE_KEYS:
            os.environ.pop(key, None)
        yield


@contextmanager
def pinned_lane(lane: str, **extra: str) -> Iterator[None]:
    """Run the block with *lane* stated, plus any *extra* env the case needs."""
    with _restored((*LANE_KEYS, *extra)):
        for key in LANE_KEYS:
            os.environ.pop(key, None)
        os.environ.update(_LANE_ENV[lane])
        os.environ.update(extra)
        assert session_lane() == lane, f"env states {session_lane()}, not {lane}"
        yield


def pin_lane(monkeypatch: pytest.MonkeyPatch, lane: str, **extra: str) -> None:
    """The :func:`pinned_lane` contract for a case that already holds a *monkeypatch*."""
    for key in LANE_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_LANE_ENV[lane], **extra}.items():
        monkeypatch.setenv(key, value)
    assert session_lane() == lane, f"env states {session_lane()}, not {lane}"
