"""Tests for management-commands doc generation — auto-generated, drift-gated."""

import json
import logging
from pathlib import Path

import pytest
from django.core.management import call_command

from teatree.core import management_commands_doc
from teatree.core.management_commands_doc import (
    ManagementCommandsDocPayload,
    build_management_commands_doc_payload,
    render_management_commands_markdown,
)


class TestBuildManagementCommandsDocPayload:
    def test_returns_typed_dict_with_commands_list(self) -> None:
        payload = build_management_commands_doc_payload()
        assert "commands" in payload
        assert isinstance(payload["commands"], list)

    def test_lifecycle_command_present(self) -> None:
        payload = build_management_commands_doc_payload()
        names = {entry["name"] for entry in payload["commands"]}
        assert "lifecycle" in names

    def test_workspace_command_present(self) -> None:
        payload = build_management_commands_doc_payload()
        names = {entry["name"] for entry in payload["commands"]}
        assert "workspace" in names

    def test_generate_all_docs_command_present(self) -> None:
        payload = build_management_commands_doc_payload()
        names = {entry["name"] for entry in payload["commands"]}
        assert "generate_all_docs" in names

    def test_tasks_session_view_excluded(self) -> None:
        """tasks_session_view is a helper module, not a real management command."""
        payload = build_management_commands_doc_payload()
        names = {entry["name"] for entry in payload["commands"]}
        assert "tasks_session_view" not in names

    def test_each_entry_has_name_help_and_subcommands(self) -> None:
        payload = build_management_commands_doc_payload()
        for entry in payload["commands"]:
            assert "name" in entry
            assert "help" in entry
            assert "subcommands" in entry
            assert isinstance(entry["subcommands"], list)

    def test_lifecycle_has_subcommands(self) -> None:
        payload = build_management_commands_doc_payload()
        lifecycle = next(e for e in payload["commands"] if e["name"] == "lifecycle")
        sub_names = [s["name"] for s in lifecycle["subcommands"]]
        assert "visit-phase" in sub_names

    def test_subcommand_has_name_and_help(self) -> None:
        payload = build_management_commands_doc_payload()
        lifecycle = next(e for e in payload["commands"] if e["name"] == "lifecycle")
        for sub in lifecycle["subcommands"]:
            assert "name" in sub
            assert "help" in sub

    def test_workspace_subcommands_include_provision(self) -> None:
        payload = build_management_commands_doc_payload()
        workspace = next(e for e in payload["commands"] if e["name"] == "workspace")
        sub_names = [s["name"] for s in workspace["subcommands"]]
        assert "provision" in sub_names

    def test_cost_command_has_empty_subcommands(self) -> None:
        """Cost is a leaf command with no subcommands."""
        payload = build_management_commands_doc_payload()
        cost = next((e for e in payload["commands"] if e["name"] == "cost"), None)
        assert cost is not None
        assert cost["subcommands"] == []

    def test_commands_sorted_alphabetically(self) -> None:
        payload = build_management_commands_doc_payload()
        names = [e["name"] for e in payload["commands"]]
        assert names == sorted(names)

    def test_payload_is_deterministic(self) -> None:
        first = build_management_commands_doc_payload()
        second = build_management_commands_doc_payload()
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


class TestRenderManagementCommandsMarkdown:
    def test_starts_with_h1_heading(self) -> None:
        payload = build_management_commands_doc_payload()
        md = render_management_commands_markdown(payload)
        assert md.startswith("# Management Commands\n")

    def test_contains_lifecycle_section(self) -> None:
        payload = build_management_commands_doc_payload()
        md = render_management_commands_markdown(payload)
        assert "## `lifecycle`" in md

    def test_contains_workspace_section(self) -> None:
        payload = build_management_commands_doc_payload()
        md = render_management_commands_markdown(payload)
        assert "## `workspace`" in md

    def test_subcommands_appear_as_table_rows(self) -> None:
        payload = build_management_commands_doc_payload()
        md = render_management_commands_markdown(payload)
        # provision is a workspace subcommand
        assert "provision" in md

    def test_ends_with_newline(self) -> None:
        payload = build_management_commands_doc_payload()
        md = render_management_commands_markdown(payload)
        assert md.endswith("\n")

    def test_minimal_payload_renders_correctly(self) -> None:
        payload: ManagementCommandsDocPayload = {
            "commands": [
                {
                    "name": "demo",
                    "help": "A demo command.",
                    "subcommands": [
                        {"name": "run", "help": "Run demo."},
                        {"name": "stop", "help": "Stop demo."},
                    ],
                }
            ]
        }
        md = render_management_commands_markdown(payload)
        assert "# Management Commands" in md
        assert "## `demo`" in md
        assert "A demo command" in md
        assert "run" in md
        assert "stop" in md

    def test_leaf_command_renders_help_without_subcommand_table(self) -> None:
        payload: ManagementCommandsDocPayload = {
            "commands": [{"name": "speak", "help": "Read text aloud.", "subcommands": []}]
        }
        md = render_management_commands_markdown(payload)
        assert "## `speak`" in md
        assert "Read text aloud" in md


class TestGenerateManagementCommandsDocCommand:
    def test_writes_markdown_file(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_management_commands_doc", output_dir=str(output_dir))
        assert (output_dir / "management-commands.md").is_file()

    def test_writes_json_file(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_management_commands_doc", output_dir=str(output_dir))
        assert (output_dir / "management-commands.json").is_file()

    def test_markdown_content_is_valid(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_management_commands_doc", output_dir=str(output_dir))
        md = (output_dir / "management-commands.md").read_text(encoding="utf-8")
        assert md.startswith("# Management Commands\n")
        assert "lifecycle" in md
        assert "workspace" in md

    def test_json_content_is_valid(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_management_commands_doc", output_dir=str(output_dir))
        payload = json.loads((output_dir / "management-commands.json").read_text(encoding="utf-8"))
        assert "commands" in payload
        names = {e["name"] for e in payload["commands"]}
        assert "lifecycle" in names

    def test_idempotent(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_management_commands_doc", output_dir=str(output_dir))
        first_md = (output_dir / "management-commands.md").read_text(encoding="utf-8")
        first_json = (output_dir / "management-commands.json").read_text(encoding="utf-8")
        call_command("generate_management_commands_doc", output_dir=str(output_dir))
        assert (output_dir / "management-commands.md").read_text(encoding="utf-8") == first_md
        assert (output_dir / "management-commands.json").read_text(encoding="utf-8") == first_json


class TestAppLabelParameterization:
    """#3356: the app label is a parameter, so overlay commands are not omitted."""

    def test_default_documents_core(self) -> None:
        names = {e["name"] for e in build_management_commands_doc_payload()["commands"]}
        assert "lifecycle" in names

    def test_a_foreign_only_label_excludes_core(self) -> None:
        payload = build_management_commands_doc_payload(app_labels=("some.other.app",))
        assert payload["commands"] == []

    def test_a_command_owned_by_a_passed_label_is_documented(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An overlay command loads under ITS OWN app label, not core's — the pin
        # that used to drop every non-``teatree.core`` command (#3356).
        monkeypatch.setattr(
            management_commands_doc,
            "get_commands",
            lambda: {"lifecycle": "teatree.core", "overlay_only": "my_overlay.app"},
        )
        loaded: list[tuple[str, str]] = []

        def _fake_load(app_label: str, name: str) -> object:
            loaded.append((app_label, name))
            return type("C", (), {"help": f"{name} help", "typer_app": None})

        monkeypatch.setattr(management_commands_doc, "load_command_class", _fake_load)

        payload = build_management_commands_doc_payload(app_labels=("teatree.core", "my_overlay.app"))
        names = {e["name"] for e in payload["commands"]}
        assert names == {"lifecycle", "overlay_only"}
        assert ("my_overlay.app", "overlay_only") in loaded

    def test_a_command_that_fails_to_import_is_logged_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(management_commands_doc, "get_commands", lambda: {"boom": "teatree.core"})

        def _raise(app_label: str, name: str) -> object:
            msg = "boom cannot import"
            raise ImportError(msg)

        monkeypatch.setattr(management_commands_doc, "load_command_class", _raise)

        with caplog.at_level(logging.WARNING, logger="teatree.core.management_commands_doc"):
            payload = build_management_commands_doc_payload()

        assert payload["commands"] == []
        assert any("boom" in rec.message for rec in caplog.records)


class TestGenerateAllDocsIncludesManagementCommands:
    def test_generate_all_docs_writes_management_commands_md(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "generated"
        call_command("generate_all_docs", output_dir=str(output_dir))
        assert (output_dir / "management-commands.md").is_file()
