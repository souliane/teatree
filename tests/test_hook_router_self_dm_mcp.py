"""Self-DM-token gate: refuse claude.ai Slack MCP writes to a bot↔user DM channel.

The claude.ai Slack MCP write tools (``slack_send_message``,
``slack_add_reaction``, ``slack_schedule_message``, ``slack_send_message_draft``)
publish under the USER's OAuth token. A post/react to the operator's own bot↔user
DM renders as user-authored and lets the loop's scanners react to the agent's own
message. The on-behalf egress class governs colleague surfaces but never sees an
MCP tool call.

The self-DM destination has two forms, mirroring the canonical
``SlackBotBackend._is_self_dm``: the configured ``D…`` DM channel id AND the
``U…`` user id (Slack accepts a user id as a ``chat.postMessage`` target that
opens the self-IM). Both are collected from config (per-overlay
``slack_dm_channel_id`` + ``slack_user_id``, and the global ``[teatree]
slack_user_id``) — never hardcoded.

This gate denies an MCP write whose destination is one of those ids and points
the caller at the bot-token path (``t3 teatree notify send -``). Posts to any
other channel pass through untouched.

Fail direction (user decision): FAIL-CLOSED. The hook cannot self-identify the
author config-free (no token/network, schema text not in the input), so an
unreadable/missing/malformed config DENIES with an error naming the toml
problem and the fix. A readable config with no ids stays ALLOW (genuinely-empty
is a real state, not an error). The ``[teatree] self_dm_gate_enabled`` kill-switch
is the sanctioned explicit disable.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router


class _FakeHomePath:
    def __init__(self, home: Path) -> None:
        self._home = home

    def __call__(self, *args: object, **kwargs: object) -> Path:
        return Path(*args, **kwargs)

    def home(self) -> Path:
        return self._home


_CONFIG_WITH_DM_CHANNELS = """
[teatree]
mode = "auto"

[overlays.t3-acme]
messaging_backend = "slack"
slack_user_id = "U0AAAAAAAAA"
slack_dm_channel_id = "D0BFIRSTDM01"

[overlays.t3-widget]
messaging_backend = "slack"
slack_user_id = "U0AAAAAAAAA"
slack_dm_channel_id = "D0BSECONDDM2"
"""

_CONFIG_NO_DM_CHANNELS = """
[teatree]
mode = "auto"

[overlays.t3-acme]
messaging_backend = "slack"
"""

# No overlay table at all — only the global [teatree] slack_user_id. The U-form
# must still deny via the global fallback (mirrors notify._resolve_user_id).
_CONFIG_GLOBAL_USER_ONLY = """
[teatree]
mode = "auto"
slack_user_id = "U0GLOBALUSER"
"""

_USER_ID = "U0AAAAAAAAA"
_DM_CHANNEL = "D0BFIRSTDM01"

_SEND = "mcp__claude_ai_Slack__slack_send_message"
_REACT = "mcp__claude_ai_Slack__slack_add_reaction"
_SCHEDULE = "mcp__claude_ai_Slack__slack_schedule_message"
_DRAFT = "mcp__claude_ai_Slack__slack_send_message_draft"


def _patch_home(home: Path, body: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    home.mkdir(exist_ok=True)
    if body is not None:
        (home / ".teatree.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(router, "Path", _FakeHomePath(home))


def _event(tool_name: str, tool_input: dict, *, session_id: str) -> dict:
    return {"session_id": session_id, "tool_name": tool_name, "tool_input": tool_input}


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestDeniesSelfDmWrites:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _CONFIG_WITH_DM_CHANNELS, monkeypatch)

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            # ── D… DM channel id form ──
            (_SEND, {"channel": "D0BFIRSTDM01", "text": "Full-day review report"}),
            (_SEND, {"channel": "D0BSECONDDM2", "text": "status"}),
            (_REACT, {"channel": "D0BFIRSTDM01", "name": "eyes", "timestamp": "1.2"}),
            # Defensive: some MCP shapes use ``channel_id`` for the destination.
            (_SEND, {"channel_id": "D0BSECONDDM2", "text": "status"}),
            # SCOPE ADD: scheduled + draft writes reproduce the incident too.
            (_SCHEDULE, {"channel": "D0BFIRSTDM01", "text": "delayed report", "post_at": "1"}),
            (_DRAFT, {"channel": "D0BSECONDDM2", "text": "draft body"}),
            # ── U… user id form (Slack opens the self-IM) — the BLOCKER ──
            (_SEND, {"channel": _USER_ID, "text": "Full-day review report"}),
            (_SEND, {"channel_id": _USER_ID, "text": "status"}),
            (_REACT, {"channel": _USER_ID, "name": "eyes", "timestamp": "1.2"}),
            (_SCHEDULE, {"channel": _USER_ID, "text": "delayed report", "post_at": "1"}),
            (_DRAFT, {"channel": _USER_ID, "text": "draft body"}),
        ],
    )
    def test_self_dm_write_is_denied(
        self, tool_name: str, tool_input: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        verdict = router.handle_block_self_dm_via_mcp(_event(tool_name, tool_input, session_id="s1"))
        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert "notify send" in deny["permissionDecisionReason"]

    def test_global_user_id_fallback_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No overlay table — only the global [teatree] slack_user_id. The U-form
        # must still deny (mirrors notify._resolve_user_id overlay→global order).
        _patch_home(tmp_path / "home2", _CONFIG_GLOBAL_USER_ONLY, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "U0GLOBALUSER", "text": "report"}, session_id="s1g")
        )
        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "notify send" in deny["permissionDecisionReason"]


class TestPassesThroughColleagueAndUnrelated:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _CONFIG_WITH_DM_CHANNELS, monkeypatch)

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            # Colleague channel — governed by the on-behalf gate, not this one.
            (_SEND, {"channel": "C0COLLEAGUE1", "text": "review note"}),
            (_REACT, {"channel": "C0COLLEAGUE1", "name": "white_check_mark", "timestamp": "1.2"}),
            # A different DM-shaped id that is NOT configured stays untouched.
            (_SEND, {"channel": "D0BUNKNOWN99", "text": "hi"}),
        ],
    )
    def test_non_dm_channel_passes_through(
        self, tool_name: str, tool_input: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        verdict = router.handle_block_self_dm_via_mcp(_event(tool_name, tool_input, session_id="s2"))
        assert verdict is False
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        "tool_name",
        [
            "Bash",
            "Edit",
            "mcp__claude_ai_Slack__slack_read_channel",
        ],
    )
    def test_non_target_tool_passes_through(self, tool_name: str, capsys: pytest.CaptureFixture[str]) -> None:
        verdict = router.handle_block_self_dm_via_mcp(
            _event(tool_name, {"channel": "D0BFIRSTDM01", "text": "x"}, session_id="s3")
        )
        assert verdict is False
        assert capsys.readouterr().out.strip() == ""


class TestFailsClosedOnUnresolvableConfig:
    # User decision: config-free self-identification isn't reliably available
    # inside the PreToolUse hook (no token/network, schema text not in the input),
    # so an unreadable/missing/malformed config DENIES — the kill-switch
    # [teatree] self_dm_gate_enabled=false is the sanctioned escape hatch.

    def test_missing_config_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_home(tmp_path / "home", None, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "D0BFIRSTDM01", "text": "report"}, session_id="s4")
        )
        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "self_dm_gate_enabled" in deny["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "body",
        [
            _CONFIG_NO_DM_CHANNELS,
            '[teatree]\nmode = "auto"\n',
            # A non-table overlay value (overlays.name = "string") is skipped, not crashed.
            '[teatree]\nmode = "auto"\n[overlays]\nbroken = "not-a-table"\n',
        ],
    )
    def test_readable_config_without_ids_allows_silently(
        self, body: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Readable config but no self-DM ids to match (an overlay without the
        # keys, no overlays table, OR a malformed non-table overlay) → ALLOW, no
        # warn. Genuinely-empty is a real state, NOT an error → not fail-closed.
        _patch_home(tmp_path / "home", body, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "D0BFIRSTDM01", "text": "report"}, session_id="s5")
        )
        assert verdict is False
        assert capsys.readouterr().out.strip() == ""

    def test_unreadable_config_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        (home / ".teatree.toml").write_text("this is = not valid toml [[[", encoding="utf-8")
        monkeypatch.setattr(router, "Path", _FakeHomePath(home))
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "D0BFIRSTDM01", "text": "report"}, session_id="s6")
        )
        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "self_dm_gate_enabled" in deny["permissionDecisionReason"]


class TestMalformedToolInputPassesThrough:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _CONFIG_WITH_DM_CHANNELS, monkeypatch)

    @pytest.mark.parametrize("tool_input", ["not-a-dict", None, ["channel", "D0BFIRSTDM01"]])
    def test_non_dict_tool_input_passes_through(self, tool_input: object, capsys: pytest.CaptureFixture[str]) -> None:
        verdict = router.handle_block_self_dm_via_mcp(
            {"session_id": "s7", "tool_name": _SEND, "tool_input": tool_input}
        )
        assert verdict is False
        assert capsys.readouterr().out.strip() == ""


class TestKillSwitch:
    def test_disabled_setting_allows_a_self_dm_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        body = _CONFIG_WITH_DM_CHANNELS.replace('mode = "auto"', 'mode = "auto"\nself_dm_gate_enabled = false')
        _patch_home(tmp_path / "home", body, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": _DM_CHANNEL, "text": "report"}, session_id="sk1")
        )
        assert verdict is False
        assert capsys.readouterr().out.strip() == ""

    def test_enabled_default_still_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No explicit flag → default ON → still denies (kill-switch is opt-out).
        _patch_home(tmp_path / "home", _CONFIG_WITH_DM_CHANNELS, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": _DM_CHANNEL, "text": "report"}, session_id="sk2")
        )
        assert verdict is True
        assert _parse_deny(capsys) is not None


class TestRegisteredInPreToolUseChain:
    def test_handler_is_registered(self) -> None:
        assert router.handle_block_self_dm_via_mcp in router._HANDLERS["PreToolUse"]
