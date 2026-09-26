"""The ORM-backed settings comparison runs through Django's command framework."""

# test-path: cross-cutting — covers the management-command boundary and its core settings collaborator

import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

import teatree.core.settings.settings_compare as settings_compare_module


class TestSettingsCompareManagementCommand(TestCase):
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
