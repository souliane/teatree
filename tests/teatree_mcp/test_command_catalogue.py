"""Tests for the `command_search` discoverability tool and its catalogue seam.

The pure ranking (`search_commands`) is exercised against an explicit catalogue
so it needs no CLI. The provider-inversion round-trip is exercised on the seam's
own register/build pair. The real end-to-end path is exercised by importing
`teatree.cli` (which registers the live catalogue provider at import time) and
searching the actual `t3` command tree through `introspection.command_search`.
"""

import pytest
from django.test import TestCase

import teatree.cli  # noqa: F401 — import registers the live command-catalogue provider
from teatree.mcp import command_catalogue, introspection
from teatree.mcp.command_catalogue import CommandRecord, search_commands


def _fixture_catalogue() -> list[CommandRecord]:
    return [
        CommandRecord(
            path="t3 teatree worktree provision",
            summary="Provision a worktree's DB and env.",
            emits_json=False,
        ),
        CommandRecord(path="t3 cost", summary="Show the session cost report.", emits_json=True),
        CommandRecord(path="t3 loop tick", summary="Run one autonomous loop tick.", emits_json=False),
    ]


class TestSearchCommands:
    def test_matches_on_path_tokens(self) -> None:
        rows = search_commands("worktree provision", catalogue=_fixture_catalogue(), limit=10)

        assert rows[0]["path"] == "t3 teatree worktree provision"

    def test_matches_on_summary_words(self) -> None:
        rows = search_commands("session cost", catalogue=_fixture_catalogue(), limit=10)

        assert any(row["path"] == "t3 cost" for row in rows)

    def test_carries_the_emits_json_flag(self) -> None:
        rows = search_commands("cost", catalogue=_fixture_catalogue(), limit=10)

        assert rows[0]["emits_json"] is True

    def test_blank_query_returns_nothing(self) -> None:
        assert search_commands("   ", catalogue=_fixture_catalogue(), limit=10) == []

    def test_no_match_returns_empty(self) -> None:
        assert search_commands("zzznotacommand", catalogue=_fixture_catalogue(), limit=10) == []

    def test_limit_caps_the_result_count(self) -> None:
        catalogue = [CommandRecord(path=f"t3 group{n} run", summary="run a thing", emits_json=False) for n in range(10)]

        rows = search_commands("run", catalogue=catalogue, limit=3)

        assert len(rows) == 3


class TestProviderInversion:
    def test_default_provider_fails_loud(self) -> None:
        with pytest.raises(RuntimeError, match="not registered"):
            command_catalogue._unregistered_provider()

    def test_register_and_build_round_trip(self) -> None:
        records = [CommandRecord(path="t3 x", summary="y", emits_json=False)]
        original = command_catalogue._provider
        command_catalogue.register_command_catalogue_provider(lambda: records)
        try:
            assert command_catalogue.build_command_catalogue() == records
        finally:
            command_catalogue.register_command_catalogue_provider(original)


class TestRealCliProvider(TestCase):
    def test_a_known_leaf_resolves_through_command_search(self) -> None:
        rows = introspection.command_search(query="mcp serve", limit=50)

        assert any(row["path"] == "t3 mcp serve" for row in rows)

    def test_a_json_emitting_command_is_flagged(self) -> None:
        rows = introspection.command_search(query="cost", limit=50)

        cost = next((row for row in rows if row["path"] == "t3 cost"), None)
        assert cost is not None
        assert cost["emits_json"] is True
