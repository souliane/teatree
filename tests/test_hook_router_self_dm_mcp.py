# test-path: cross-cutting — the gate spans hook_router.py (hooks/) and teatree.config.host_projection.
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
unreadable config store DENIES with an error naming the config-store problem and
the fix. A readable store with no ids stays ALLOW (genuinely-empty is a real
state, not an error). An ABSENT canonical DB is not unreadable on a host — the
control DB lives in a container volume and the ids come from the published host
projection, exactly as the id reads themselves resolve them. The ``[teatree]
self_dm_gate_enabled`` kill-switch is the sanctioned explicit disable.
"""

import json
import sqlite3
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.config.host_projection import ProjectionPublisher


def _seed_config_db(path: Path, rows: dict[str, object]) -> None:
    """Seed the DB-home ``teatree_config_setting`` store the self-DM gate resolves."""
    conn = sqlite3.connect(str(path))
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


_ROWS_WITH_DM_CHANNELS: dict[str, object] = {
    "overlays": {
        "t3-acme": {
            "messaging_backend": "slack",
            "slack_user_id": "U0AAAAAAAAA",
            "slack_dm_channel_id": "D0BFIRSTDM01",
        },
        "t3-widget": {
            "messaging_backend": "slack",
            "slack_user_id": "U0AAAAAAAAA",
            "slack_dm_channel_id": "D0BSECONDDM2",
        },
    },
}

_ROWS_NO_DM_CHANNELS: dict[str, object] = {"overlays": {"t3-acme": {"messaging_backend": "slack"}}}

# No overlay ids at all — only the global slack_user_id. The U-form must still
# deny via the global fallback (mirrors notify.resolve_user_id).
_ROWS_GLOBAL_USER_ONLY: dict[str, object] = {"slack_user_id": "U0GLOBALUSER"}

_USER_ID = "U0AAAAAAAAA"
_DM_CHANNEL = "D0BFIRSTDM01"

_SEND = "mcp__claude_ai_Slack__slack_send_message"
_REACT = "mcp__claude_ai_Slack__slack_add_reaction"
_SCHEDULE = "mcp__claude_ai_Slack__slack_schedule_message"
_DRAFT = "mcp__claude_ai_Slack__slack_send_message_draft"


def _point_at_seeded_db(db: Path, rows: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_config_db(db, rows)
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def _point_at_host_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, canonical_db: Path | None = None) -> Path:
    """A host's view: the canonical control DB unreachable, the data dir it projects into empty.

    Returns the data dir, so a caller can publish a projection into it.
    """
    absent = tmp_path / "control-db-volume" / "db.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(canonical_db if canonical_db is not None else absent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    data_dir = tmp_path / "share" / "teatree"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _project_onto_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, object], *, canonical_db: Path | None = None
) -> None:
    """Publish *rows* as the host projection, from a source DB only the container side can open."""
    data_dir = _point_at_host_view(tmp_path, monkeypatch, canonical_db=canonical_db)
    source = tmp_path / "source-db.sqlite3"
    _seed_config_db(source, rows)
    conn = sqlite3.connect(str(source))
    try:
        # The publisher projects the loop-state table unguarded, so a source without it raises.
        conn.execute("CREATE TABLE teatree_loop_state (id INTEGER PRIMARY KEY, name TEXT, status TEXT)")
        conn.commit()
    finally:
        conn.close()
    ProjectionPublisher(source, data_dir).publish()


def _event(tool_name: str, tool_input: dict, *, session_id: str) -> dict:
    return {"session_id": session_id, "tool_name": tool_name, "tool_input": tool_input}


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestDeniesSelfDmWrites:
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _point_at_seeded_db(tmp_path / "db.sqlite3", _ROWS_WITH_DM_CHANNELS, monkeypatch)

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
        # No overlay ids — only the global slack_user_id. The U-form must still
        # deny (mirrors notify.resolve_user_id overlay→global order).
        _point_at_seeded_db(tmp_path / "db2.sqlite3", _ROWS_GLOBAL_USER_ONLY, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "U0GLOBALUSER", "text": "report"}, session_id="s1g")
        )
        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "notify send" in deny["permissionDecisionReason"]


class TestPassesThroughColleagueAndUnrelated:
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _point_at_seeded_db(tmp_path / "db.sqlite3", _ROWS_WITH_DM_CHANNELS, monkeypatch)

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
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "D0BFIRSTDM01", "text": "report"}, session_id="s4")
        )
        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "self_dm_gate_enabled" in deny["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "rows",
        [
            _ROWS_NO_DM_CHANNELS,
            {"mode": "auto"},
            # A non-table overlay value (overlays.name = "string") is skipped, not crashed.
            {"overlays": {"broken": "not-a-table"}},
        ],
    )
    def test_readable_config_without_ids_allows_silently(
        self,
        rows: dict[str, object],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Readable store but no self-DM ids to match (an overlay without the keys,
        # no overlays row, OR a malformed non-table overlay) → ALLOW, no warn.
        # Genuinely-empty is a real state, NOT an error → not fail-closed.
        _point_at_seeded_db(tmp_path / "db.sqlite3", rows, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "D0BFIRSTDM01", "text": "report"}, session_id="s5")
        )
        assert verdict is False
        assert capsys.readouterr().out.strip() == ""

    def test_unreadable_config_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corrupt = tmp_path / "db.sqlite3"
        corrupt.write_bytes(b"this is not a sqlite database at all")
        monkeypatch.setenv("T3_CONFIG_DB", str(corrupt))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "D0BFIRSTDM01", "text": "report"}, session_id="s6")
        )
        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "self_dm_gate_enabled" in deny["permissionDecisionReason"]


class TestResolvesFromTheHostProjection:
    """The ordinary HOST case: the control DB is in a container volume, only the projection is readable.

    The id reads (``mapping_setting`` / ``str_setting``) already fall through to the
    published projection when the canonical DB file is absent. The reachability probe
    did not, so it answered "unreachable" for a store the very next lines read the ids
    out of — and the gate fail-closed on every colleague-channel write.
    """

    @pytest.fixture(autouse=True)
    def _projected_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _project_onto_host(tmp_path, monkeypatch, _ROWS_WITH_DM_CHANNELS)

    def test_ids_resolve_from_the_projection(self) -> None:
        destinations = router._self_dm_destination_ids()

        assert destinations.resolved is True
        assert destinations.ids == frozenset({_USER_ID, "D0BFIRSTDM01", "D0BSECONDDM2"})

    def test_global_user_id_resolves_from_the_projection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _project_onto_host(tmp_path / "another-host", monkeypatch, _ROWS_GLOBAL_USER_ONLY)

        assert router._self_dm_destination_ids().ids == frozenset({"U0GLOBALUSER"})

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            (_SEND, {"channel": "C0COLLEAGUE1", "text": "review note"}),
            (_REACT, {"channel": "C0COLLEAGUE1", "name": "eyes", "timestamp": "1.2"}),
            (_SCHEDULE, {"channel": "C0COLLEAGUE1", "text": "later", "post_at": "1"}),
        ],
    )
    def test_colleague_channel_write_passes_through(
        self, tool_name: str, tool_input: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        verdict = router.handle_block_self_dm_via_mcp(_event(tool_name, tool_input, session_id="p1"))

        assert verdict is False
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize("destination", [_DM_CHANNEL, "D0BSECONDDM2", _USER_ID])
    def test_self_dm_write_is_still_denied(self, destination: str, capsys: pytest.CaptureFixture[str]) -> None:
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": destination, "text": "report"}, session_id="p2")
        )

        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "notify send" in deny["permissionDecisionReason"]


class TestStaysClosedWithoutAProjectionToReadFrom:
    """The projection is a fallback for an ABSENT canonical DB, never a second chance at a broken one."""

    def test_no_projection_published_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _point_at_host_view(tmp_path, monkeypatch)

        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": _DM_CHANNEL, "text": "report"}, session_id="p3")
        )

        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "self_dm_gate_enabled" in deny["permissionDecisionReason"]

    def test_corrupt_canonical_db_is_denied_even_with_a_projection_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A PRESENT but unreadable canonical DB is what the id reads use, and they get
        # nothing from it — no projection fallback applies — so an empty id set here is a
        # read failure, not a genuinely-empty config, and the gate must stay closed.
        corrupt = tmp_path / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a sqlite database at all")
        _project_onto_host(tmp_path, monkeypatch, _ROWS_WITH_DM_CHANNELS, canonical_db=corrupt)

        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": "C0COLLEAGUE1", "text": "review note"}, session_id="p4")
        )

        assert verdict is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "self_dm_gate_enabled" in deny["permissionDecisionReason"]


class TestMalformedToolInputPassesThrough:
    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _point_at_seeded_db(tmp_path / "db.sqlite3", _ROWS_WITH_DM_CHANNELS, monkeypatch)

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
        rows = {**_ROWS_WITH_DM_CHANNELS, "self_dm_gate_enabled": False}
        _point_at_seeded_db(tmp_path / "db.sqlite3", rows, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": _DM_CHANNEL, "text": "report"}, session_id="sk1")
        )
        assert verdict is False
        assert capsys.readouterr().out.strip() == ""

    def test_enabled_default_still_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # No explicit flag → default ON → still denies (kill-switch is opt-out).
        _point_at_seeded_db(tmp_path / "db.sqlite3", _ROWS_WITH_DM_CHANNELS, monkeypatch)
        verdict = router.handle_block_self_dm_via_mcp(
            _event(_SEND, {"channel": _DM_CHANNEL, "text": "report"}, session_id="sk2")
        )
        assert verdict is True
        assert _parse_deny(capsys) is not None


class TestRegisteredInPreToolUseChain:
    def test_handler_is_registered(self) -> None:
        assert router.handle_block_self_dm_via_mcp in router._HANDLERS["PreToolUse"]
