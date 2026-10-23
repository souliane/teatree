"""The ORM-backed settings comparison runs through Django's command framework."""

# test-path: cross-cutting — covers the management-command boundary and its core settings collaborator

import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from typer.testing import CliRunner

import teatree.core.settings.settings_compare as settings_compare_module
from teatree.cli import app
from teatree.core.settings.settings_peers import PeerSnapshot
from teatree.core.settings_snapshot import SNAPSHOT_FORMAT


class TestSettingsCompareManagementCommand(TestCase):
    def test_terminal_filters_select_the_same_rows_as_the_page(self) -> None:
        def box(label: str, value: int) -> PeerSnapshot:
            return PeerSnapshot(
                label=label,
                url="" if label == "this instance" else "http://127.0.0.1/peer",
                note="",
                payload={
                    "format": SNAPSHOT_FORMAT,
                    "format_version": 2,
                    "fingerprint": {},
                    "registry": {
                        "settings": {
                            "merge_wip": {"family": "setting", "group": "Agents"},
                            "overlays": {"family": "registry", "group": "Registries"},
                        }
                    },
                    "values": {
                        "settings": {"": {"merge_wip": value, "overlays": {}}},
                        "defaults": {},
                        "provenance": {},
                        "seed": {},
                        "seed_shipped": {},
                    },
                },
            )

        with (
            patch.object(settings_compare_module, "local_snapshot", return_value=box("this instance", 2)),
            patch.object(settings_compare_module, "peer_snapshots", return_value=(box("peer", 3),)),
        ):
            page = Client().get(
                reverse("dash:settings_compare"),
                {"family": "setting", "group": "Agents", "text": "merge", "differs_only": "off"},
                REMOTE_ADDR="127.0.0.1",
            )
            terminal = CliRunner().invoke(
                app,
                [
                    "settings",
                    "compare",
                    "--family",
                    "setting",
                    "--group",
                    "Agents",
                    "--text",
                    "merge",
                    "--all-rows",
                    "--json",
                ],
            )

        assert page.status_code == 200
        assert terminal.exit_code == 0, terminal.output
        page_keys = [(row.scope, row.title) for row in page.context["comparison"].rows]
        terminal_keys = [(row["scope"], row["key"]) for row in json.loads(terminal.stdout)["rows"]]
        assert page_keys == terminal_keys == [("", "merge_wip")]

    def test_filters_select_the_same_rows_as_the_page(self) -> None:
        registry = {
            "merge_wip": {"family": "setting", "group": "Agents"},
            "autoload": {"family": "setting", "group": "Agents"},
            "overlays": {"family": "registry", "group": "Registries"},
        }

        def box(label: str, merge_wip: int) -> PeerSnapshot:
            return PeerSnapshot(
                label=label,
                url="" if label == "local" else "http://127.0.0.1/peer",
                note="",
                payload={
                    "format": SNAPSHOT_FORMAT,
                    "format_version": 2,
                    "fingerprint": {},
                    "registry": {"settings": registry},
                    "values": {
                        "settings": {"": {"merge_wip": merge_wip, "autoload": False, "overlays": {}}},
                        "defaults": {},
                        "provenance": {},
                        "seed": {},
                        "seed_shipped": {},
                    },
                },
            )

        stdout = StringIO()
        with (
            patch.object(settings_compare_module, "local_snapshot", return_value=box("local", 2)),
            patch.object(settings_compare_module, "peer_snapshots", return_value=(box("peer", 3),)),
        ):
            call_command(
                "settings_compare",
                family="setting",
                group="Agents",
                text="",
                scope="global",
                all_rows=True,
                json_output=True,
                stdout=stdout,
            )

        assert [(row["key"], row["kind"]) for row in json.loads(stdout.getvalue())["rows"]] == [
            ("merge_wip", "values differ"),
            ("autoload", "no difference"),
        ], stdout.getvalue()

    def test_the_human_view_uses_the_machine_output_seam(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.object(settings_compare_module, "peer_snapshots", return_value=()),
            pytest.raises(SystemExit) as stopped,
        ):
            call_command("settings_compare", stdout=stdout, stderr=stderr)

        assert stopped.value.code == 1
        assert stdout.getvalue() == ""
        assert "no reachable peer to compare against" in stderr.getvalue()

    def test_the_json_view_is_the_only_stdout_document(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.object(settings_compare_module, "peer_snapshots", return_value=()),
            pytest.raises(SystemExit) as stopped,
        ):
            call_command("settings_compare", json_output=True, stdout=stdout, stderr=stderr)

        assert stopped.value.code == 1
        assert json.loads(stdout.getvalue())["error"].startswith("no reachable peer to compare against")
        assert stderr.getvalue() == ""
