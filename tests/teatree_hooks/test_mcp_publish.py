"""MCP publish bodies: the Slack roster and the Notion connector's default-deny-by-verb classifier (#5023)."""

import pytest

from hooks.scripts.mcp_slack_write_guard import is_slack_mcp_write
from teatree.hooks.mcp_publish import (
    extract_mcp_publish_payload,
    is_notion_mcp_fetch,
    is_notion_mcp_tool,
    is_notion_mcp_write,
    notion_write_text,
    values_under_keys,
)

PAGE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"

SERVERS = ("claude_ai_Notion", "notion", "plugin_notion-workspace_notion", "Notion")

READ_TOOLS = (
    "notion-fetch",
    "notion-search",
    "notion-get-comments",
    "notion-get-users",
    "notion-get-teams",
    "API-retrieve-a-page",
    "API-retrieve-a-block",
    "API-post-search",
    "API-get-block-children",
)
ROSTER_WRITE_TOOLS = (
    "notion-create-pages",
    "notion-update-page",
    "notion-move-pages",
    "notion-duplicate-page",
    "notion-create-database",
    "notion-update-database",
    "notion-create-comment",
    "API-patch-block-children",
    "API-patch-page",
    "API-post-page",
    "API-update-a-block",
    "API-delete-a-block",
    "API-create-a-comment",
)
UNSEEN_WRITE_TOOLS = ("notion-append-block", "notionAppendBlock")


class TestNotionToolClassification:
    @pytest.mark.parametrize("server", SERVERS)
    @pytest.mark.parametrize("tool", READ_TOOLS)
    def test_known_read_is_a_notion_tool_but_not_a_write(self, server: str, tool: str) -> None:
        name = f"mcp__{server}__{tool}"
        assert is_notion_mcp_tool(name)
        assert not is_notion_mcp_write(name)

    @pytest.mark.parametrize("server", SERVERS)
    @pytest.mark.parametrize("tool", ROSTER_WRITE_TOOLS)
    def test_every_roster_write_is_a_write(self, server: str, tool: str) -> None:
        assert is_notion_mcp_write(f"mcp__{server}__{tool}")

    @pytest.mark.parametrize("tool", UNSEEN_WRITE_TOOLS)
    def test_unseen_tool_is_a_write_because_only_a_read_verb_clears_it(self, tool: str) -> None:
        assert is_notion_mcp_write(f"mcp__notion__{tool}")

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__slack__slack_send_message",
            "mcp__claude_ai_Slack__slack_search_public",
            "mcp__glab__glab_mr_create",
            "Bash",
            "Write",
        ],
    )
    def test_other_tools_are_not_notion_writes(self, tool_name: str) -> None:
        assert not is_notion_mcp_tool(tool_name)
        assert not is_notion_mcp_write(tool_name)

    @pytest.mark.parametrize("tool_name", ["mcp__notion__notion-fetch", "mcp__Notion__API-retrieve-a-page"])
    def test_a_direct_fetch_can_stamp_a_page(self, tool_name: str) -> None:
        assert is_notion_mcp_fetch(tool_name)

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__notion__notion-search",
            "mcp__notion__notion-get-comments",
            "mcp__notion__notion-update-page",
            "mcp__slack__slack_read_thread",
        ],
    )
    def test_search_comments_writes_and_other_servers_cannot_stamp_a_page(self, tool_name: str) -> None:
        assert not is_notion_mcp_fetch(tool_name)

    def test_slack_guard_and_notion_classifier_never_claim_the_same_tool(self) -> None:
        names = [
            f"mcp__{server}__{tool}"
            for server in SERVERS
            for tool in (*READ_TOOLS, *ROSTER_WRITE_TOOLS, *UNSEEN_WRITE_TOOLS)
        ]
        assert [name for name in names if is_slack_mcp_write(name)] == []

    @pytest.mark.parametrize(
        "tool",
        ["notion-create-view", "notion-update-view", "notion-read-and-archive", "notionRetrieveAndUpdate"],
    )
    def test_a_write_verb_wins_over_a_read_verb(self, tool: str) -> None:
        name = f"mcp__notion__{tool}"
        assert is_notion_mcp_write(name)
        assert not is_notion_mcp_fetch(name)


class TestNotionWriteText:
    def test_an_id_property_is_prose_not_the_target_id(self) -> None:
        data = {"page_id": PAGE, "properties": {"id": "a quoted or secret value"}}
        assert notion_write_text("mcp__notion__notion-update-page", data) == "a quoted or secret value"

    def test_old_str_is_not_published_text(self) -> None:
        data = {
            "page_id": PAGE,
            "data": {"content_updates": [{"old_str": "a quote being removed", "new_str": "clean"}]},
        }
        assert notion_write_text("mcp__notion__notion-update-page", data) == "clean"

    def test_an_old_str_property_outside_content_updates_is_still_scanned(self) -> None:
        data = {"page_id": PAGE, "properties": {"old_str": "published property"}}
        assert notion_write_text("mcp__notion__notion-update-page", data) == "published property"

    def test_nested_edit_text_is_exposed_and_the_page_id_is_not(self) -> None:
        tool_input = {
            "data": {
                "page_id": PAGE,
                "command": "update_content",
                "content_updates": [{"old_str": "before sentence", "new_str": "after sentence"}],
            }
        }
        text = notion_write_text("mcp__notion__notion-update-page", tool_input)
        assert text is not None
        assert "before sentence" not in text
        assert "after sentence" in text
        assert PAGE not in text

    def test_parent_page_id_and_block_children_are_split_into_identity_and_prose(self) -> None:
        tool_input = {
            "parent": {"page_id": PAGE},
            "pages": [{"properties": {"title": "A title"}, "content": "Body text"}],
        }
        text = notion_write_text("mcp__notion__notion-create-pages", tool_input)
        assert text is not None
        assert "A title" in text
        assert "Body text" in text
        assert PAGE not in text

    @pytest.mark.parametrize("tool_input", [{}, {"page_id": PAGE}, {"page_id": PAGE, "block_id": PAGE}])
    def test_a_write_with_no_body_is_the_empty_string_not_none(self, tool_input: dict) -> None:
        assert notion_write_text("mcp__notion__notion-duplicate-page", tool_input) == ""

    def test_a_read_has_no_publish_text(self) -> None:
        assert notion_write_text("mcp__notion__notion-fetch", {"id": PAGE, "query": "text"}) is None

    def test_deeply_nested_prose_is_still_exposed(self) -> None:
        deep: dict = {"leaf": "buried sentence"}
        for _ in range(200):
            deep = {"next": [deep]}
        text = notion_write_text("mcp__notion__notion-update-page", deep)
        assert text == "buried sentence"


class TestExtractMcpPublishPayload:
    def test_notion_write_yields_its_text(self) -> None:
        payload = extract_mcp_publish_payload("mcp__notion__notion-create-comment", {"page_id": PAGE, "text": "hi"})
        assert payload == "hi"

    def test_notion_read_yields_none(self) -> None:
        assert extract_mcp_publish_payload("mcp__notion__notion-search", {"query": "x"}) is None

    @pytest.mark.parametrize(
        ("tool_name", "tool_input", "expected"),
        [
            ("mcp__slack__slack_send_message", {"text": "hello", "channel": "C1"}, "hello"),
            ("mcp__slack__slack_send_message", {"message": "fallback body"}, "fallback body"),
            ("mcp__slack__slack_create_canvas", {"document_content": "canvas"}, "canvas"),
            ("mcp__slack__slack_send_message", {"channel": "C1"}, ""),
            ("mcp__slack__slack_get_channel_history", {"channel": "C1"}, None),
            ("mcp__other__send_message", {"text": "hello"}, None),
            ("Write", {"content": "hello"}, None),
        ],
    )
    def test_slack_extraction_is_unchanged(self, tool_name: str, tool_input: dict, expected: str | None) -> None:
        assert extract_mcp_publish_payload(tool_name, tool_input) == expected


class TestValuesUnderKeys:
    def test_collects_matching_strings_at_any_depth_and_skips_other_shapes(self) -> None:
        tool_input = {
            "page_id": PAGE,
            "parent": {"page_id": "parent-id", "database_id": "db"},
            "pages": [{"page_id": "in-list"}, {"page_id": 7}, {"title": "page_id"}],
        }
        assert sorted(values_under_keys(tool_input, frozenset({"page_id"}))) == sorted(["in-list", "parent-id", PAGE])

    def test_a_matching_key_holding_a_mapping_is_still_searched(self) -> None:
        assert values_under_keys({"page_id": {"page_id": "inner"}}, frozenset({"page_id"})) == ["inner"]
