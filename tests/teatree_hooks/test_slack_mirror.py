"""Slack transport leaf for the AskUserQuestion mirror (extracted from hook_router).

Ports the transport-level coverage that lived in
``tests/teatree_core/models/test_slack_mirror_hook.py`` (channel cache,
``slack_post_message`` ts contract, ``slack_post_dm`` cache/open/thread flow)
onto the new ``teatree.hooks.slack_mirror`` leaf, and pins the #1110/#2384
design: the leaf is a pure ``teatree.hooks`` (platform) leaf — the Slack
``post`` (``conversations.open`` idempotent, ``chat.postMessage`` NOT
idempotent) and the active-DM-thread resolver are INJECTED, so the leaf never
imports ``teatree.backends.slack`` / ``teatree.core`` (a backwards layer edge).
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from teatree.hooks import slack_mirror
from teatree.hooks.slack_mirror import slack_config_from_registry


def _no_thread(_channel: str) -> str:
    return ""


class TestSlackPostMessageInjectsPoster:
    """The transport calls the injected poster with the right idempotency class."""

    def test_open_dm_posts_conversations_open_idempotent(self) -> None:
        poster = MagicMock(return_value={"ok": True, "channel": {"id": "D-open"}})
        cid = slack_mirror.slack_open_dm(poster, "tok", "U1")
        assert cid == "D-open"
        poster.assert_called_once_with("conversations.open", token="tok", json={"users": "U1"}, idempotent=True)

    def test_open_dm_empty_on_missing_channel(self) -> None:
        poster = MagicMock(return_value={"ok": True})
        assert slack_mirror.slack_open_dm(poster, "tok", "U1") == ""

    def test_open_dm_empty_on_transport_error(self) -> None:
        poster = MagicMock(side_effect=RuntimeError("down"))
        assert slack_mirror.slack_open_dm(poster, "tok", "U1") == ""

    def test_post_message_posts_chat_postmessage_non_idempotent(self) -> None:
        poster = MagicMock(return_value={"ok": True, "ts": "1700.0001"})
        ts = slack_mirror.slack_post_message(poster, "D1", "hi", bot_token="tok")
        assert ts == "1700.0001"
        poster.assert_called_once_with(
            "chat.postMessage", token="tok", json={"channel": "D1", "text": "hi"}, idempotent=False
        )

    def test_post_message_threads_under_thread_ts(self) -> None:
        poster = MagicMock(return_value={"ok": True, "ts": "1700.0002"})
        slack_mirror.slack_post_message(poster, "D1", "hi", bot_token="tok", thread_ts="1700.0000")
        _name, kwargs = poster.call_args
        assert kwargs["json"] == {"channel": "D1", "text": "hi", "thread_ts": "1700.0000"}

    def test_post_message_empty_on_not_ok(self) -> None:
        poster = MagicMock(return_value={"ok": False, "error": "channel_not_found"})
        assert slack_mirror.slack_post_message(poster, "D1", "hi", bot_token="tok") == ""

    def test_post_message_empty_on_transport_error(self) -> None:
        poster = MagicMock(side_effect=RuntimeError("down"))
        assert slack_mirror.slack_post_message(poster, "D1", "hi", bot_token="tok") == ""


class TestDmChannelCache:
    def test_round_trip_through_cache_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert slack_mirror.read_dm_channel_cache("U1") == ""
        slack_mirror.write_dm_channel_cache("U1", "D123")
        assert slack_mirror.read_dm_channel_cache("U1") == "D123"

    def test_multiple_users_in_same_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        slack_mirror.write_dm_channel_cache("U1", "D1")
        slack_mirror.write_dm_channel_cache("U2", "D2")
        assert slack_mirror.read_dm_channel_cache("U1") == "D1"
        assert slack_mirror.read_dm_channel_cache("U2") == "D2"

    def test_corrupt_cache_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        path = slack_mirror.slack_dm_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert slack_mirror.read_dm_channel_cache("U1") == ""


class TestSlackPostDm:
    def test_cache_hit_skips_open_dm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        slack_mirror.write_dm_channel_cache("U1", "D-cached")
        with (
            patch.object(slack_mirror, "slack_open_dm") as mock_open,
            patch.object(slack_mirror, "slack_post_message", return_value="1700.1") as mock_post,
        ):
            slack_mirror.slack_post_dm(MagicMock(), _no_thread, "xoxb-tok", "U1", "hello")
        mock_open.assert_not_called()
        _poster, channel, text = mock_post.call_args.args
        assert (channel, text) == ("D-cached", "hello")
        assert mock_post.call_args.kwargs == {"bot_token": "xoxb-tok", "thread_ts": ""}

    def test_cache_miss_opens_dm_and_caches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with (
            patch.object(slack_mirror, "slack_open_dm", return_value="D-new") as mock_open,
            patch.object(slack_mirror, "slack_post_message", return_value="1700.1") as mock_post,
        ):
            slack_mirror.slack_post_dm(MagicMock(), _no_thread, "xoxb-tok", "U1", "hello")
        mock_open.assert_called_once()
        _poster, channel, text = mock_post.call_args.args
        assert (channel, text) == ("D-new", "hello")
        assert slack_mirror.read_dm_channel_cache("U1") == "D-new"

    def test_stale_cache_falls_back_to_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        slack_mirror.write_dm_channel_cache("U1", "D-stale")
        with (
            patch.object(slack_mirror, "slack_open_dm", return_value="D-fresh") as mock_open,
            patch.object(slack_mirror, "slack_post_message", side_effect=["", "1700.1"]) as mock_post,
        ):
            slack_mirror.slack_post_dm(MagicMock(), _no_thread, "xoxb-tok", "U1", "hello")
        mock_open.assert_called_once()
        assert mock_post.call_count == 2
        assert slack_mirror.read_dm_channel_cache("U1") == "D-fresh"

    def test_cache_hit_threads_under_active_dm_thread(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        slack_mirror.write_dm_channel_cache("U1", "D-cached")
        resolver = MagicMock(return_value="1700000000.0009")
        with patch.object(slack_mirror, "slack_post_message", return_value="1700.1") as mock_post:
            slack_mirror.slack_post_dm(MagicMock(), resolver, "xoxb-tok", "U1", "hello")
        resolver.assert_called_once_with("D-cached")
        assert mock_post.call_args.kwargs == {"bot_token": "xoxb-tok", "thread_ts": "1700000000.0009"}

    def test_opened_channel_threads_under_active_dm_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        resolver = MagicMock(return_value="1700000000.0042")
        with (
            patch.object(slack_mirror, "slack_open_dm", return_value="D-new"),
            patch.object(slack_mirror, "slack_post_message", return_value="1700.1") as mock_post,
        ):
            slack_mirror.slack_post_dm(MagicMock(), resolver, "xoxb-tok", "U1", "hello")
        resolver.assert_called_once_with("D-new")
        assert mock_post.call_args.kwargs == {"bot_token": "xoxb-tok", "thread_ts": "1700000000.0042"}


def _ok_token(*_a: object, **_k: object) -> object:
    return type("R", (), {"returncode": 0, "stdout": "xoxb-tok"})()


class TestAudioEnrichment:
    """``perform_slack_post`` fires the injected audio enricher AFTER a successful post (#2171).

    Anti-vacuous: before the change the mirror never called any enricher, so
    ``test_enriches_delivered_channel`` (asserting it IS called with the
    delivered channel) fails on the pre-change code.
    """

    def test_enriches_delivered_channel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        slack_mirror.write_dm_channel_cache("U1", "D-cached")
        monkeypatch.setattr(slack_mirror, "run_allowed_to_fail", _ok_token)
        enrich = MagicMock()
        with patch.object(slack_mirror, "slack_post_message", return_value="1700.1"):
            slack_mirror.perform_slack_post(
                ("ref", "U1"),
                [{"question": "Ship?"}],
                poster=MagicMock(),
                resolve_thread=lambda _c: "1700.0000",
                enrich_audio=enrich,
            )
        channel, text, thread_ts = enrich.call_args.args
        assert channel == "D-cached"
        assert "Ship?" in text
        assert thread_ts == "1700.0000"

    def test_enriches_freshly_opened_channel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        monkeypatch.setattr(slack_mirror, "run_allowed_to_fail", _ok_token)
        enrich = MagicMock()
        with (
            patch.object(slack_mirror, "slack_open_dm", return_value="D-new"),
            patch.object(slack_mirror, "slack_post_message", return_value="1700.1"),
        ):
            slack_mirror.perform_slack_post(
                ("ref", "U1"),
                [{"question": "Ship?"}],
                poster=MagicMock(),
                resolve_thread=_no_thread,
                enrich_audio=enrich,
            )
        assert enrich.call_args.args[0] == "D-new"

    def test_no_enricher_is_text_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        slack_mirror.write_dm_channel_cache("U1", "D-cached")
        monkeypatch.setattr(slack_mirror, "run_allowed_to_fail", _ok_token)
        with patch.object(slack_mirror, "slack_post_message", return_value="1700.1"):
            ts = slack_mirror.perform_slack_post(
                ("ref", "U1"), [{"question": "Ship?"}], poster=MagicMock(), resolve_thread=_no_thread
            )
        assert ts == "1700.1"  # just the text post, no enricher, no raise

    def test_enricher_not_invoked_when_post_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        monkeypatch.setattr(slack_mirror, "run_allowed_to_fail", _ok_token)
        enrich = MagicMock()
        with (
            patch.object(slack_mirror, "slack_open_dm", return_value="D-new"),
            patch.object(slack_mirror, "slack_post_message", return_value=""),
        ):
            slack_mirror.perform_slack_post(
                ("ref", "U1"),
                [{"question": "Ship?"}],
                poster=MagicMock(),
                resolve_thread=_no_thread,
                enrich_audio=enrich,
            )
        enrich.assert_not_called()

    def test_enricher_failure_never_breaks_the_post(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        slack_mirror.write_dm_channel_cache("U1", "D-cached")
        monkeypatch.setattr(slack_mirror, "run_allowed_to_fail", _ok_token)
        enrich = MagicMock(side_effect=RuntimeError("synthesis blew up"))
        with patch.object(slack_mirror, "slack_post_message", return_value="1700.1"):
            ts = slack_mirror.perform_slack_post(
                ("ref", "U1"),
                [{"question": "Ship?"}],
                poster=MagicMock(),
                resolve_thread=_no_thread,
                enrich_audio=enrich,
            )
        assert ts == "1700.1"  # the text question still lands

    def test_enrich_skipped_when_channel_uncached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))  # empty cache
        enrich = MagicMock()
        slack_mirror._enrich_delivered_dm(enrich, _no_thread, "U-unknown", "hi")
        enrich.assert_not_called()


class TestQuestionFormatting:
    def test_formats_question_with_numbered_options(self) -> None:
        text = slack_mirror.format_question_text(
            [{"question": "Ship it?", "options": [{"label": "Yes", "description": "go"}, {"label": "No"}]}]
        )
        assert "*Ship it?*" in text
        assert "1. Yes — go" in text
        assert "2. No" in text
        assert "Reply with the number" in text

    def test_string_option_does_not_raise(self) -> None:
        # A bare-string option (loose harness input) must not AttributeError on
        # ``opt.get`` — a raise here means the question DM never lands.
        text = slack_mirror.format_question_text([{"question": "Ship it?", "options": ["Yes", "No"]}])
        assert "1. Yes" in text
        assert "2. No" in text

    def test_non_mapping_question_and_bad_options_are_skipped(self) -> None:
        text = slack_mirror.format_question_text(["not-a-dict", {"question": "Q?", "options": "not-a-list"}])
        assert "*Q?*" in text  # the valid question still renders; the junk is skipped


class TestWriteCacheNeverRaises:
    def test_unwritable_cache_dir_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A cache path whose PARENT is an existing FILE makes mkdir raise
        # NotADirectoryError (an OSError). The write must swallow it, not
        # propagate into the never-raise mirror.
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        monkeypatch.setattr(slack_mirror, "slack_dm_cache_path", lambda: blocker / "teatree" / "cache.json")
        slack_mirror.write_dm_channel_cache("U1", "D1")  # must not raise


class TestConfigFromRegistry:
    def test_returns_ref_and_uid_for_slack_overlay(self) -> None:
        overlays = {
            "acme": {"messaging_backend": "slack", "slack_token_ref": "secret/acme", "slack_user_id": "U9"},
        }
        fake_cfg = SimpleNamespace(raw={"overlays": overlays})
        with patch("teatree.config.load_config", return_value=fake_cfg):
            assert slack_config_from_registry() == ("secret/acme", "U9")

    def test_none_when_no_overlays(self) -> None:
        fake_cfg = SimpleNamespace(raw={})
        with patch("teatree.config.load_config", return_value=fake_cfg):
            assert slack_config_from_registry() is None

    def test_none_when_no_slack_overlay(self) -> None:
        fake_cfg = SimpleNamespace(raw={"overlays": {"acme": {"messaging_backend": "console"}}})
        with patch("teatree.config.load_config", return_value=fake_cfg):
            assert slack_config_from_registry() is None


class TestPerformSlackPostInjectsDependencies:
    def test_returns_empty_when_token_unavailable(self) -> None:
        result = MagicMock(returncode=1, stdout="")
        with patch.object(slack_mirror, "run_allowed_to_fail", return_value=result):
            ts = slack_mirror.perform_slack_post(
                ("ref", "U1"), [{"question": "Q"}], poster=MagicMock(), resolve_thread=_no_thread
            )
        assert ts == ""

    def test_posts_dm_with_injected_poster_and_resolver(self) -> None:
        result = MagicMock(returncode=0, stdout="xoxb-tok\n")
        poster = MagicMock()
        with (
            patch.object(slack_mirror, "run_allowed_to_fail", return_value=result) as mock_run,
            patch.object(slack_mirror, "slack_post_dm", return_value="1700.5") as mock_dm,
        ):
            ts = slack_mirror.perform_slack_post(
                ("ref", "U1"), [{"question": "Q"}], poster=poster, resolve_thread=_no_thread
            )
        assert ts == "1700.5"
        assert mock_run.call_args.args[0] == ["pass", "show", "ref-bot"]
        passed_poster, passed_resolver, bot_token, user_id, _text = mock_dm.call_args.args
        assert passed_poster is poster
        assert passed_resolver is _no_thread
        assert (bot_token, user_id) == ("xoxb-tok", "U1")
