"""The single ``slack_user_channel`` resolver shared by every DM-channel caller (#126).

Before this resolver existed, the live-post-approval CLI carried its own
private ``_user_channel`` copy of the overlay→global→empty config walk
that :func:`teatree.core.notify.resolve_user_id` already implemented for
``slack_user_id``. Two copies of the same resolution order is a
config-trap-via-drift: a fix to one (a new precedence rule, a typo'd key)
silently diverges from the other.

These tests pin the contract of the canonical
:func:`teatree.core.notify.resolve_user_channel`:

* it reads ``slack_user_channel`` with the SAME overlay→global→empty order
    :func:`resolve_user_id` uses for ``slack_user_id``;
* the live-post-approval CLI resolves the channel through it (no private
    duplicate), so both DM-channel call sites agree on one channel id.
"""

import json
import sqlite3

from teatree.cli.review import live_approval
from teatree.core import notify
from teatree.core.notify import resolve_user_channel


def _write_cfg(
    tmp_path,
    monkeypatch,
    *,
    global_user_id: str = "",
    global_channel: str = "",
    overlays: dict | None = None,
) -> None:
    """Seed the DB-home slack routing config (global keys + ``overlays`` registry)."""
    rows: dict[str, object] = {}
    if global_user_id:
        rows["slack_user_id"] = global_user_id
    if global_channel:
        rows["slack_user_channel"] = global_channel
    if overlays:
        rows["overlays"] = overlays
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


class TestResolveUserChannel:
    """``resolve_user_channel`` mirrors ``resolve_user_id`` resolution order."""

    def test_global_channel_is_read(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(tmp_path, monkeypatch, global_channel="D-GLOBAL")

        assert notify.resolve_user_channel() == "D-GLOBAL"

    def test_overlay_channel_overrides_global(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        _write_cfg(
            tmp_path,
            monkeypatch,
            global_channel="D-GLOBAL",
            overlays={"acme": {"slack_user_channel": "D-OVERLAY"}},
        )

        assert notify.resolve_user_channel() == "D-OVERLAY"

    def test_overlay_without_channel_falls_back_to_global(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        _write_cfg(
            tmp_path,
            monkeypatch,
            global_channel="D-GLOBAL",
            overlays={"acme": {"slack_user_id": "U-X"}},
        )

        assert notify.resolve_user_channel() == "D-GLOBAL"

    def test_no_channel_configured_is_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(tmp_path, monkeypatch, global_user_id="U-X")

        assert notify.resolve_user_channel() == ""

    def test_same_resolution_order_as_user_id(self, tmp_path, monkeypatch) -> None:
        # The user-id resolver and the channel resolver walk the identical
        # overlay→global→empty path: an overlay override wins for BOTH keys.
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        _write_cfg(
            tmp_path,
            monkeypatch,
            global_user_id="U-GLOBAL",
            global_channel="D-GLOBAL",
            overlays={"acme": {"slack_user_id": "U-OVERLAY", "slack_user_channel": "D-OVERLAY"}},
        )

        assert notify.resolve_user_id() == "U-OVERLAY"
        assert notify.resolve_user_channel() == "D-OVERLAY"


class TestSoleOverlayFallback:
    """Env-independent fallback: a single registered overlay resolves without ``T3_OVERLAY_NAME``.

    The headless worker that DMs the owner does NOT export ``T3_OVERLAY_NAME``,
    so the overlay-scoped tier is skipped; a fresh box carries no GLOBAL
    ``slack_user_id`` / ``slack_user_channel``. When exactly one overlay is
    registered there is no ambiguity, so its own values resolve — mirroring
    ``backend_factory.messaging_from_overlay``'s single-overlay resolution.
    """

    def test_user_id_resolves_from_sole_overlay_without_env_or_global(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(tmp_path, monkeypatch, overlays={"acme": {"slack_user_id": "U-SOLE"}})

        assert notify.resolve_user_id() == "U-SOLE"

    def test_channel_resolves_from_sole_overlay_without_env_or_global(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(tmp_path, monkeypatch, overlays={"acme": {"slack_user_channel": "D-SOLE"}})

        assert notify.resolve_user_channel() == "D-SOLE"

    def test_global_still_wins_over_sole_overlay(self, tmp_path, monkeypatch) -> None:
        # The sole-overlay tier is the LAST resort — a configured global keeps priority.
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(
            tmp_path,
            monkeypatch,
            global_user_id="U-GLOBAL",
            global_channel="D-GLOBAL",
            overlays={"acme": {"slack_user_id": "U-SOLE", "slack_user_channel": "D-SOLE"}},
        )

        assert notify.resolve_user_id() == "U-GLOBAL"
        assert notify.resolve_user_channel() == "D-GLOBAL"

    def test_multiple_overlays_stay_ambiguous(self, tmp_path, monkeypatch) -> None:
        # Two registered overlays with no env selector and no global: never guess —
        # a multi-overlay box must not silently pick the wrong owner.
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(
            tmp_path,
            monkeypatch,
            overlays={
                "acme": {"slack_user_id": "U-ACME", "slack_user_channel": "D-ACME"},
                "beta": {"slack_user_id": "U-BETA", "slack_user_channel": "D-BETA"},
            },
        )

        assert notify.resolve_user_id() == ""
        assert notify.resolve_user_channel() == ""


class TestLiveApprovalCliUsesCanonicalResolver:
    """The live-post-approval CLI resolves its DM channel through the canonical helper."""

    def test_no_private_duplicate_resolver(self) -> None:
        # The duplicate ``_user_channel`` in review.live_approval is gone:
        # callers point at the single notify.resolve_user_channel.
        assert not hasattr(live_approval, "_user_channel")

    def test_cli_channel_lookup_delegates_to_notify(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        _write_cfg(
            tmp_path,
            monkeypatch,
            global_channel="D-GLOBAL",
            overlays={"acme": {"slack_user_channel": "D-OVERLAY"}},
        )

        assert resolve_user_channel() == "D-OVERLAY"
