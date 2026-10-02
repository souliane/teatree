"""Enabled-MCP connectivity + provider check (souliane/teatree#2282)."""

import json
from pathlib import Path

from teatree.core.mcp_connectivity import (
    CLAUDE_AI_HOSTED,
    THIRD_PARTY,
    ConfiguredMcpServer,
    McpConnectivityOutcome,
    McpServerStatus,
    check_mcp_connectivity,
    parse_mcp_list_output,
    read_enabled_mcp_servers,
    read_ever_connected,
    resolve_provider,
)


def _write_claude_json(home: Path, payload: dict) -> None:
    (home / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")


class TestReadEverConnected:
    def test_returns_the_ever_connected_set(self, tmp_path):
        _write_claude_json(tmp_path, {"claudeAiMcpEverConnected": ["claude.ai Slack", "claude.ai Notion"]})
        assert read_ever_connected(home=tmp_path) == {"claude.ai Slack", "claude.ai Notion"}

    def test_missing_file_is_empty_set(self, tmp_path):
        assert read_ever_connected(home=tmp_path) == set()

    def test_absent_key_is_empty_set(self, tmp_path):
        _write_claude_json(tmp_path, {"mcpServers": {"glab": {"type": "stdio"}}})
        assert read_ever_connected(home=tmp_path) == set()

    def test_malformed_json_is_empty_set(self, tmp_path):
        (tmp_path / ".claude.json").write_text("{ not json", encoding="utf-8")
        assert read_ever_connected(home=tmp_path) == set()

    def test_non_dict_root_is_empty_set(self, tmp_path):
        (tmp_path / ".claude.json").write_text("[]", encoding="utf-8")
        assert read_ever_connected(home=tmp_path) == set()


class TestReadEnabledMcpServers:
    def test_enabled_is_configured_minus_disabled(self, tmp_path):
        _write_claude_json(
            tmp_path,
            {
                "mcpServers": {
                    "GitHub": {"type": "stdio", "command": "gh"},
                    "glab": {"type": "stdio", "command": "glab"},
                },
                "claudeAiMcpEverConnected": ["claude.ai Notion"],
                "projects": {
                    str(tmp_path): {"disabledMcpServers": ["glab"]},
                },
            },
        )
        enabled = read_enabled_mcp_servers(home=tmp_path, cwd=tmp_path)
        names = {s.name for s in enabled}
        assert "GitHub" in names
        assert "claude.ai Notion" in names
        assert "glab" not in names

    def test_claude_ai_connector_is_claude_ai_hosted(self, tmp_path):
        _write_claude_json(tmp_path, {"claudeAiMcpEverConnected": ["claude.ai Slack"]})
        enabled = read_enabled_mcp_servers(home=tmp_path, cwd=tmp_path)
        assert enabled == [ConfiguredMcpServer(name="claude.ai Slack", provider=CLAUDE_AI_HOSTED)]

    def test_local_stdio_server_is_third_party(self, tmp_path):
        _write_claude_json(tmp_path, {"mcpServers": {"glab": {"type": "stdio"}}})
        enabled = read_enabled_mcp_servers(home=tmp_path, cwd=tmp_path)
        assert enabled == [ConfiguredMcpServer(name="glab", provider=THIRD_PARTY)]

    def test_missing_file_is_empty_not_error(self, tmp_path):
        assert read_enabled_mcp_servers(home=tmp_path, cwd=tmp_path) == []

    def test_disabled_resolved_from_nearest_ancestor_project_key(self, tmp_path):
        """A worktree cwd inherits the disabled set from the nearest ANCESTOR project key.

        Claude keys ``projects`` by absolute path and a worktree cwd is rarely an
        exact key — disabled servers are declared on the parent workspace. Resolving
        by exact ``get(cwd)`` only would resolve the disabled set EMPTY and flag a
        healthy-but-disabled server as enabled-but-disconnected.
        """
        workspace = tmp_path
        worktree = workspace / "ticket-1234" / "repo"
        worktree.mkdir(parents=True)
        _write_claude_json(
            workspace,
            {
                "claudeAiMcpEverConnected": ["claude.ai Notion", "claude.ai Figma"],
                "projects": {
                    str(workspace): {"disabledMcpServers": ["claude.ai Figma"]},
                },
            },
        )
        names = {s.name for s in read_enabled_mcp_servers(home=workspace, cwd=worktree)}
        assert "claude.ai Notion" in names
        assert "claude.ai Figma" not in names

    def test_exact_project_key_wins_over_shorter_ancestor(self, tmp_path):
        """The longest-prefix (nearest) ancestor key wins, not merely any ancestor."""
        workspace = tmp_path
        worktree = workspace / "ticket-1234" / "repo"
        worktree.mkdir(parents=True)
        _write_claude_json(
            workspace,
            {
                "claudeAiMcpEverConnected": ["claude.ai Notion", "claude.ai Figma"],
                "projects": {
                    str(workspace): {"disabledMcpServers": ["claude.ai Notion"]},
                    str(worktree): {"disabledMcpServers": ["claude.ai Figma"]},
                },
            },
        )
        names = {s.name for s in read_enabled_mcp_servers(home=workspace, cwd=worktree)}
        assert "claude.ai Notion" in names
        assert "claude.ai Figma" not in names


class TestResolveProvider:
    def test_claude_ai_prefix_resolves_hosted(self):
        assert resolve_provider("claude.ai Notion", url="https://mcp.notion.com/mcp") == CLAUDE_AI_HOSTED

    def test_plain_local_name_resolves_third_party(self):
        assert resolve_provider("glab", url="") == THIRD_PARTY


class TestParseMcpListOutput:
    def test_parses_connected_and_failed(self):
        text = (
            "Checking MCP server health…\n\n"
            "claude.ai Notion: https://mcp.notion.com/mcp - ✔ Connected\n"
            "glab: stdio - ✘ Failed to connect\n"
            "Figma: https://mcp.figma.com/mcp - ⏸ Pending approval\n"
        )
        statuses = parse_mcp_list_output(text)
        by_name = {s.name: s for s in statuses}
        assert by_name["claude.ai Notion"].connected is True
        assert by_name["glab"].connected is False
        assert by_name["Figma"].connected is False
        assert by_name["claude.ai Notion"].url == "https://mcp.notion.com/mcp"

    def test_not_connected_status_is_not_a_false_positive(self):
        """A 'Not Connected' status must NOT substring-match 'Connected' (anchored on ✔)."""
        text = "glab: stdio - Not Connected\nNotion: https://mcp.notion.com/mcp - ✔ Connected\n"
        by_name = {s.name: s for s in parse_mcp_list_output(text)}
        assert by_name["glab"].connected is False
        assert by_name["Notion"].connected is True


class TestCheckMcpConnectivity:
    def test_all_enabled_connected_is_clean(self, tmp_path):
        _write_claude_json(tmp_path, {"claudeAiMcpEverConnected": ["claude.ai Notion"]})
        outcome = check_mcp_connectivity(
            home=tmp_path,
            cwd=tmp_path,
            probe=lambda: [McpServerStatus(name="claude.ai Notion", url="x", connected=True)],
            provider_expectations={},
        )
        assert outcome.ok is True
        assert outcome.findings == []

    def test_enabled_but_disconnected_is_loud_finding(self, tmp_path):
        """Anti-vacuous: a configured-but-disconnected enabled MCP must FAIL loudly."""
        _write_claude_json(tmp_path, {"claudeAiMcpEverConnected": ["claude.ai Notion"]})
        outcome = check_mcp_connectivity(
            home=tmp_path,
            cwd=tmp_path,
            probe=lambda: [McpServerStatus(name="claude.ai Notion", url="x", connected=False)],
            provider_expectations={},
        )
        assert outcome.ok is False
        joined = " ".join(outcome.findings)
        assert "claude.ai Notion" in joined
        assert "reconnect" in joined.lower() or "re-auth" in joined.lower()

    def test_enabled_server_absent_from_probe_is_disconnected(self, tmp_path):
        _write_claude_json(tmp_path, {"claudeAiMcpEverConnected": ["claude.ai Notion"]})
        outcome = check_mcp_connectivity(
            home=tmp_path,
            cwd=tmp_path,
            probe=list,
            provider_expectations={},
        )
        assert outcome.ok is False
        assert "claude.ai Notion" in " ".join(outcome.findings)

    def test_provider_mismatch_is_loud_finding(self, tmp_path):
        _write_claude_json(tmp_path, {"claudeAiMcpEverConnected": ["claude.ai Notion"]})
        outcome = check_mcp_connectivity(
            home=tmp_path,
            cwd=tmp_path,
            probe=lambda: [McpServerStatus(name="claude.ai Notion", url="x", connected=True)],
            provider_expectations={"claude.ai Notion": THIRD_PARTY},
        )
        assert outcome.ok is False
        joined = " ".join(outcome.findings)
        assert "claude.ai Notion" in joined
        assert THIRD_PARTY in joined

    def test_probe_failure_degrades_to_warn_not_crash(self, tmp_path):
        _write_claude_json(tmp_path, {"claudeAiMcpEverConnected": ["claude.ai Notion"]})

        def _boom() -> list[McpServerStatus]:
            message = "claude binary missing"
            raise RuntimeError(message)

        outcome = check_mcp_connectivity(
            home=tmp_path,
            cwd=tmp_path,
            probe=_boom,
            provider_expectations={},
        )
        assert outcome.ok is True
        assert outcome.degraded is True

    def test_no_enabled_servers_is_clean(self, tmp_path):
        _write_claude_json(tmp_path, {})
        outcome = check_mcp_connectivity(
            home=tmp_path,
            cwd=tmp_path,
            probe=list,
            provider_expectations={},
        )
        assert outcome.ok is True
        assert isinstance(outcome, McpConnectivityOutcome)
