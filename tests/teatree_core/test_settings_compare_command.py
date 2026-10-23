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
    def _assert_single_terminal_filter_matches_page(
        self,
        *,
        filter_spec: tuple[str, str],
        settings: dict[str, dict[str, str]],
        local_values: dict[str, dict[str, object]],
        peer_values: dict[str, dict[str, object]],
        expected: list[tuple[str, str]],
    ) -> None:
        flag, value = filter_spec

        def box(label: str, values: dict[str, dict[str, object]]) -> PeerSnapshot:
            return PeerSnapshot(
                label=label,
                url="" if label == "this instance" else "http://127.0.0.1/peer",
                note="",
                payload={
                    "format": SNAPSHOT_FORMAT,
                    "format_version": 2,
                    "fingerprint": {},
                    "registry": {"settings": settings},
                    "values": {
                        "settings": values,
                        "defaults": {},
                        "provenance": {},
                        "seed": {},
                        "seed_shipped": {},
                    },
                },
            )

        with (
            patch.object(settings_compare_module, "local_snapshot", return_value=box("this instance", local_values)),
            patch.object(settings_compare_module, "peer_snapshots", return_value=(box("peer", peer_values),)),
        ):
            page = Client().get(
                reverse("dash:settings_compare"),
                {flag: value, "differs_only": "off"},
                REMOTE_ADDR="127.0.0.1",
            )
            terminal = CliRunner().invoke(app, ["settings", "compare", f"--{flag}", value, "--all-rows", "--json"])

        assert page.status_code == 200
        assert terminal.exit_code == 0, terminal.output
        page_keys = [(row.scope, row.title) for row in page.context["comparison"].rows]
        terminal_keys = [(row["scope"], row["key"]) for row in json.loads(terminal.stdout)["rows"]]
        assert page_keys == terminal_keys == expected

    def test_family_alone_selects_the_same_rows_as_the_page(self) -> None:
        self._assert_single_terminal_filter_matches_page(
            filter_spec=("family", "registry"),
            settings={
                "merge_wip": {"family": "setting", "group": "Agents"},
                "overlays": {"family": "registry", "group": "Registries"},
            },
            local_values={"": {"merge_wip": 2, "overlays": {"left": 1}}},
            peer_values={"": {"merge_wip": 3, "overlays": {"right": 1}}},
            expected=[("", "overlays")],
        )

    def test_group_alone_selects_the_same_rows_as_the_page(self) -> None:
        self._assert_single_terminal_filter_matches_page(
            filter_spec=("group", "Agents"),
            settings={
                "merge_wip": {"family": "setting", "group": "Agents"},
                "agent_max_turns": {"family": "setting", "group": "Limits"},
            },
            local_values={"": {"merge_wip": 2, "agent_max_turns": 5}},
            peer_values={"": {"merge_wip": 3, "agent_max_turns": 6}},
            expected=[("", "merge_wip")],
        )

    def test_text_alone_selects_the_same_rows_as_the_page(self) -> None:
        self._assert_single_terminal_filter_matches_page(
            filter_spec=("text", "merge"),
            settings={
                "merge_wip": {"family": "setting", "group": "Agents"},
                "autoload": {"family": "setting", "group": "Agents"},
            },
            local_values={"": {"merge_wip": 2, "autoload": False}},
            peer_values={"": {"merge_wip": 3, "autoload": True}},
            expected=[("", "merge_wip")],
        )

    def test_scope_alone_selects_the_same_rows_as_the_page(self) -> None:
        self._assert_single_terminal_filter_matches_page(
            filter_spec=("scope", "alpha"),
            settings={"merge_wip": {"family": "setting", "group": "Agents"}},
            local_values={"": {"merge_wip": 2}, "alpha": {"merge_wip": 4}},
            peer_values={"": {"merge_wip": 3}, "alpha": {"merge_wip": 5}},
            expected=[("alpha", "merge_wip")],
        )

    def test_all_rows_heading_counts_differences_only(self) -> None:
        def box(label: str, merge_wip: int) -> PeerSnapshot:
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
                            "autoload": {"family": "setting", "group": "Agents"},
                        }
                    },
                    "values": {
                        "settings": {"": {"merge_wip": merge_wip, "autoload": False}},
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
            stderr = StringIO()
            call_command("settings_compare", all_rows=True, stderr=stderr)

        assert "Differences (1)" in stderr.getvalue()
        assert "1 no difference" in stderr.getvalue()

    def test_kind_filter_heading_counts_only_listed_differences(self) -> None:
        def box(label: str, values: dict[str, object]) -> PeerSnapshot:
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
                            "autoload": {"family": "setting", "group": "Agents"},
                        }
                    },
                    "values": {
                        "settings": {"": values},
                        "defaults": {},
                        "provenance": {},
                        "seed": {},
                        "seed_shipped": {},
                    },
                },
            )

        with (
            patch.object(
                settings_compare_module,
                "local_snapshot",
                return_value=box("this instance", {"merge_wip": 2, "autoload": False}),
            ),
            patch.object(
                settings_compare_module,
                "peer_snapshots",
                return_value=(box("peer", {"merge_wip": 3}),),
            ),
        ):
            result = CliRunner().invoke(app, ["settings", "compare", "--kind", "values"])

        assert result.exit_code == 0, result.output
        assert "Differences (1)  1 values differ" in result.stderr
        assert "autoload" not in result.stderr

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
