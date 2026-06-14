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

from teatree.cli.review import live_approval
from teatree.core import notify
from teatree.core.notify import resolve_user_channel


def _write_cfg(tmp_path, monkeypatch, body: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class TestResolveUserChannel:
    """``resolve_user_channel`` mirrors ``resolve_user_id`` resolution order."""

    def test_global_channel_is_read(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(tmp_path, monkeypatch, '[teatree]\nslack_user_channel = "D-GLOBAL"\n')

        assert notify.resolve_user_channel() == "D-GLOBAL"

    def test_overlay_channel_overrides_global(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        _write_cfg(
            tmp_path,
            monkeypatch,
            '[teatree]\nslack_user_channel = "D-GLOBAL"\n\n[overlays.acme]\nslack_user_channel = "D-OVERLAY"\n',
        )

        assert notify.resolve_user_channel() == "D-OVERLAY"

    def test_overlay_without_channel_falls_back_to_global(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        _write_cfg(
            tmp_path,
            monkeypatch,
            '[teatree]\nslack_user_channel = "D-GLOBAL"\n\n[overlays.acme]\nslack_user_id = "U-X"\n',
        )

        assert notify.resolve_user_channel() == "D-GLOBAL"

    def test_no_channel_configured_is_empty(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_cfg(tmp_path, monkeypatch, '[teatree]\nslack_user_id = "U-X"\n')

        assert notify.resolve_user_channel() == ""

    def test_same_resolution_order_as_user_id(self, tmp_path, monkeypatch) -> None:
        # The user-id resolver and the channel resolver walk the identical
        # overlay→global→empty path: an overlay override wins for BOTH keys.
        monkeypatch.setenv("T3_OVERLAY_NAME", "acme")
        _write_cfg(
            tmp_path,
            monkeypatch,
            "[teatree]\n"
            'slack_user_id = "U-GLOBAL"\n'
            'slack_user_channel = "D-GLOBAL"\n\n'
            "[overlays.acme]\n"
            'slack_user_id = "U-OVERLAY"\n'
            'slack_user_channel = "D-OVERLAY"\n',
        )

        assert notify.resolve_user_id() == "U-OVERLAY"
        assert notify.resolve_user_channel() == "D-OVERLAY"


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
            '[teatree]\nslack_user_channel = "D-GLOBAL"\n\n[overlays.acme]\nslack_user_channel = "D-OVERLAY"\n',
        )

        assert resolve_user_channel() == "D-OVERLAY"
