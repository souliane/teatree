"""The seed write endpoint: one loop / preset / schedule field, through the interchange seams.

The comparison page lists every seed difference between two boxes and, until this endpoint,
offered a control for none of them — while five of the nine interchange seed fields had no
editor anywhere else in the dashboard either. The write reuses
``classify_seed_field`` + ``write_seed_field``, so what a seed field may hold has one answer
here and on the import path rather than two.
"""

# test-path: cross-cutting — exercises the dash endpoint through the core seed writer and persisted seed models

import json
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase
from django.urls import reverse

from teatree.core.models import Loop, Mode, ModeSchedule
from teatree.core.settings import seed_editing

_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


class SeedWriteEndpointTestCase(TestCase):
    def setUp(self) -> None:
        self.loop, _ = Loop.objects.get_or_create(
            name="review", defaults={"script": "teatree.loops.review", "delay_seconds": 300}
        )
        Loop.objects.filter(pk=self.loop.pk).update(description="", colleague_facing=False, delay_seconds=300)
        self.loop.refresh_from_db()

    def _post(self, table: str, name: str, field: str, value: object):
        url = reverse("dash:settings_seed_set", args=[table, name, field])
        return self.client.post(url, {"value": json.dumps(value)}, **_LOOPBACK)

    def test_a_loops_description_is_written(self) -> None:
        assert self._post("loops", "review", "description", "what it does now").status_code == 204
        self.loop.refresh_from_db()
        assert self.loop.description == "what it does now"

    def test_a_loops_colleague_facing_flag_is_written(self) -> None:
        assert self._post("loops", "review", "colleague_facing", value=True).status_code == 204
        self.loop.refresh_from_db()
        assert self.loop.colleague_facing is True

    def test_a_field_a_dedicated_editor_owns_is_refused_and_names_it(self) -> None:
        assert self.loop.delay_seconds == 300
        response = self._post("loops", "review", "delay_seconds", 30)
        assert response.status_code == 400
        assert "loops page" in response.content.decode()
        self.loop.refresh_from_db()
        assert self.loop.delay_seconds == 300

    def test_a_field_the_interchange_does_not_carry_is_refused(self) -> None:
        response = self._post("loops", "review", "enabled", value=True)
        assert response.status_code == 400
        assert "interchange does not carry" in response.content.decode()

    def test_an_entry_the_shipped_file_does_not_carry_is_refused(self) -> None:
        Loop.objects.get_or_create(name="not-shipped", defaults={"script": "teatree.loops.review", "delay_seconds": 60})
        response = self._post("loops", "not-shipped", "description", "x")
        assert response.status_code == 400
        assert "unknown loops entry" in response.content.decode()

    def test_a_value_of_the_wrong_type_is_refused(self) -> None:
        response = self._post("loops", "review", "description", 7)
        assert response.status_code == 400
        assert "expected str" in response.content.decode()
        self.loop.refresh_from_db()
        assert self.loop.description == ""

    def test_a_non_json_body_is_refused(self) -> None:
        url = reverse("dash:settings_seed_set", args=["loops", "review", "description"])
        response = self.client.post(url, {"value": "not-json"}, **_LOOPBACK)
        assert response.status_code == 400
        assert "invalid JSON" in response.content.decode()

    def test_the_write_is_audited(self) -> None:
        with self.assertLogs("teatree.dash.audit", level="INFO") as logs:
            self._post("loops", "review", "description", "audited")
        assert any("loops.review.description" in line for line in logs.output)

    def test_a_get_is_refused(self) -> None:
        url = reverse("dash:settings_seed_set", args=["loops", "review", "description"])
        assert self.client.get(url, **_LOOPBACK).status_code == 405

    def test_a_non_loopback_caller_is_refused(self) -> None:
        url = reverse("dash:settings_seed_set", args=["loops", "review", "description"])
        assert self.client.post(url, {"value": '"x"'}, REMOTE_ADDR="10.0.0.9").status_code == 403

    def test_an_unexpected_database_failure_surfaces(self) -> None:
        with (
            patch.object(seed_editing, "write_seed_field", side_effect=OperationalError("database unavailable")),
            pytest.raises(OperationalError),
        ):
            self._post("loops", "review", "description", "x")


class SeedWriteValidatesTheColumnItWritesTestCase(TestCase):
    """The classifier checks the TYPE; the column's own validators check the rest.

    `egress` is a choice field and `Mode.forbids_egress` is what decides whether the box posts
    on the owner's behalf, so a string outside its choices would be stored and then read as
    ALLOW by every consumer. `write_seed_field` is the one writer both this endpoint and
    `config_setting import` go through, so the check belongs there rather than here.
    """

    def setUp(self) -> None:
        self.preset, _ = Mode.objects.get_or_create(name="afk", defaults={"entries": {}})
        Mode.objects.filter(pk=self.preset.pk).update(entries={}, egress="allow")
        self.preset.refresh_from_db()

    def _post(self, field: str, value: object):
        url = reverse("dash:settings_seed_set", args=["modes", "afk", field])
        return self.client.post(url, {"value": json.dumps(value)}, **_LOOPBACK)

    def test_a_value_outside_the_columns_choices_is_refused(self) -> None:
        response = self._post("egress", "banana")
        assert response.status_code == 400
        self.preset.refresh_from_db()
        assert self.preset.egress == "allow"

    def test_a_value_inside_the_columns_choices_is_written(self) -> None:
        assert self._post("egress", "forbid").status_code == 204
        self.preset.refresh_from_db()
        assert self.preset.egress == "forbid"

    def test_a_presets_entries_table_is_refused_here_and_names_its_editor(self) -> None:
        # Totality is repaired by the preset editor's own write seam; a raw write here would
        # store a partial table that reads every unnamed loop OFF.
        response = self._post("entries", {"review": True})
        assert response.status_code == 400
        assert "preset" in response.content.decode()
        self.preset.refresh_from_db()
        assert self.preset.entries == {}


class SeedWriteReachesSchedulesTestCase(TestCase):
    def setUp(self) -> None:
        self.schedule, _ = ModeSchedule.objects.get_or_create(name="standard")
        ModeSchedule.objects.filter(pk=self.schedule.pk).update(timezone="")
        self.schedule.refresh_from_db()

    def test_a_schedules_timezone_is_written(self) -> None:
        url = reverse("dash:settings_seed_set", args=["schedules", "standard", "timezone"])
        assert self.client.post(url, {"value": '"Europe/Vienna"'}, **_LOOPBACK).status_code == 204
        self.schedule.refresh_from_db()
        assert self.schedule.timezone == "Europe/Vienna"
