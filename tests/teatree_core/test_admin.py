"""Django admin registrations for core models.

The autonomous-loop control plane (#1796) is manageable from the Django admin —
``Loop`` rows (name / prompt / delay / enabled) are added, edited, enabled, and
disabled there.
"""

import datetime as dt
import json

import django.http
import django.test
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse

from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot, Prompt


def _prompt(name: str = "demo-prompt") -> Prompt:
    """A reusable :class:`Prompt` FK target for loops under test (#2513)."""
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


class TestConfigSettingAdmin:
    def test_config_setting_registered_in_admin(self) -> None:
        assert ConfigSetting in admin.site._registry

    def test_config_setting_admin_lists_and_edits_value(self) -> None:
        model_admin = admin.site._registry[ConfigSetting]
        assert "key" in model_admin.list_display
        assert "scope" in model_admin.list_display
        assert "value" in model_admin.list_editable


class TestConfigSettingAdminSaves(django.test.TestCase):
    """An empty list/dict is a legitimate override, so the admin must be able to save it.

    ``ConfigSetting`` is a generic key/value store with no per-key arity, and
    ``statusline_chain = []`` means "override the shipped non-empty default with
    nothing". These tests POST through the real admin views — the coverage gap
    that let a blanket non-empty requirement on the storage field ship.
    """

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser("admin-cfg", "cfg@example.com", "pw")
        self.client.force_login(user)

    @staticmethod
    def _change_form_post(row: ConfigSetting, value: str) -> dict[str, str]:
        return {"scope": row.scope, "key": row.key, "value": value, "seeded_by": "", "seed_value": ""}

    def _post_change_form(self, row: ConfigSetting, value: str) -> django.http.HttpResponse:
        url = reverse("admin:core_configsetting_change", args=[row.pk])
        return self.client.post(url, self._change_form_post(row, value))

    @staticmethod
    def _change_form_errors(response: django.http.HttpResponse) -> dict[str, list[str]]:
        return dict(response.context["adminform"].form.errors) if response.status_code == 200 else {}

    def test_change_form_saves_an_empty_list_value(self) -> None:
        row = ConfigSetting.objects.set_value("statusline_chain", [])
        response = self._post_change_form(row, "[]")
        assert response.status_code == 302, self._change_form_errors(response)
        row.refresh_from_db()
        assert row.value == []

    def test_change_form_saves_an_empty_dict_value(self) -> None:
        row = ConfigSetting.objects.set_value("agent_skill_models", {})
        response = self._post_change_form(row, "{}")
        assert response.status_code == 302, self._change_form_errors(response)
        row.refresh_from_db()
        assert row.value == {}

    def test_changelist_saves_every_row_at_its_current_value(self) -> None:
        """The real-world failure: ``list_editable`` makes the whole page ONE formset.

        A single rejected empty row fails ``formset.is_valid()`` and NOTHING on
        the page saves, so re-submitting unchanged live rows must be a no-op
        save, not a page-wide validation failure.
        """
        rows = [
            ConfigSetting.objects.set_value("statusline_chain", []),
            ConfigSetting.objects.set_value("banned_terms_allowlist", []),
            ConfigSetting.objects.set_value("agent_skill_models", {}),
            ConfigSetting.objects.set_value("mode", "auto"),
        ]
        data = {
            "form-TOTAL_FORMS": str(len(rows)),
            "form-INITIAL_FORMS": str(len(rows)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        for index, row in enumerate(rows):
            data[f"form-{index}-id"] = str(row.pk)
            data[f"form-{index}-value"] = json.dumps(row.value)

        response = self.client.post(reverse("admin:core_configsetting_changelist"), data)

        errors = response.context["cl"].formset.errors if response.status_code == 200 else []
        assert response.status_code == 302, errors
        for row in rows:
            expected = row.value
            row.refresh_from_db()
            assert row.value == expected

    def test_change_form_rejects_an_empty_value_with_a_field_error(self) -> None:
        """An empty textarea is a form error, never a NOT NULL ``IntegrityError``.

        ``None`` is the resolver's "no row, use the default" sentinel and the
        column is NOT NULL, so a blank submission must be refused at the form
        layer — the hole that ``blank=True`` alone would open.
        """
        row = ConfigSetting.objects.set_value("statusline_chain", ["branch"])
        response = self._post_change_form(row, "")
        assert response.status_code == 200
        assert "value" in self._change_form_errors(response)
        row.refresh_from_db()
        assert row.value == ["branch"]


class TestLoopAdmin(django.test.TestCase):
    def test_loop_registered_in_admin(self) -> None:
        assert Loop in admin.site._registry

    def test_loop_admin_lists_key_columns(self) -> None:
        model_admin = admin.site._registry[Loop]
        for column in ("name", "enabled", "colleague_facing", "action", "run_in_sub_agent", "description", "cadence"):
            assert column in model_admin.list_display

    def test_loop_admin_colleague_facing_is_editable(self) -> None:
        model_admin = admin.site._registry[Loop]
        assert "colleague_facing" in model_admin.list_editable

    def test_loop_admin_action_shows_script_or_prompt(self) -> None:
        model_admin = admin.site._registry[Loop]
        prompt_loop = Loop(name="demo-prompt", delay_seconds=60, prompt=_prompt())
        script_loop = Loop(name="demo-script", delay_seconds=60, prompt=None, script="run.py")
        assert model_admin.action(prompt_loop) == "do x"
        assert model_admin.action(script_loop) == "run.py"

    def test_loop_admin_cadence_shows_human_label(self) -> None:
        model_admin = admin.site._registry[Loop]
        loop = Loop(name="demo-cadence", delay_seconds=60, prompt=_prompt())
        assert model_admin.cadence(loop) == "every 60s"

    def test_loop_admin_allows_inline_enable_disable(self) -> None:
        model_admin = admin.site._registry[Loop]
        assert "enabled" in model_admin.list_editable


class TestPresetScheduleAdminRegistered:
    """LP-4: the preset + schedule models are editable from the Django admin.

    The plan promised an admin surface for presets and slot editing, but the
    four #3159 models had no ``ModelAdmin`` — leaving slot times/days/preset only
    editable by a raw DB write.
    """

    def test_loop_preset_registered(self) -> None:
        assert Mode in admin.site._registry

    def test_loop_preset_override_registered(self) -> None:
        assert ModeOverride in admin.site._registry

    def test_loop_schedule_registered(self) -> None:
        assert ModeSchedule in admin.site._registry

    def test_loop_schedule_slot_registered(self) -> None:
        assert ModeScheduleSlot in admin.site._registry

    def test_slots_editable_inline_under_schedule(self) -> None:
        # The cheapest slot-editing surface: a slot inline under its schedule so
        # days/start_time/preset are edited in place without a standalone add.
        model_admin = admin.site._registry[ModeSchedule]
        inline_models = [inline.model for inline in model_admin.inlines]
        assert ModeScheduleSlot in inline_models
        slot_inline = next(inline for inline in model_admin.inlines if inline.model is ModeScheduleSlot)
        for field in ("days", "start_time", "preset_name"):
            assert field in slot_inline.fields


class TestPresetScheduleAdminChangelistsLoad(django.test.TestCase):
    """LP-4 smoke test: each new admin changelist renders for a superuser (HTTP 200).

    Loads the actual changelist through the admin client so a misconfigured
    ``list_display`` / inline would surface as a non-200, not just a registry hit.
    """

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser("admin-lp4", "lp4@example.com", "pw")
        self.client.force_login(user)

    def _assert_changelist_loads(self, model: type) -> None:
        url = reverse(f"admin:core_{model._meta.model_name}_changelist")
        assert self.client.get(url).status_code == 200

    def test_loop_preset_changelist_loads(self) -> None:
        Mode.objects.create(name="heads-down", entries={"review": False})
        self._assert_changelist_loads(Mode)

    def test_loop_preset_override_changelist_loads(self) -> None:
        ModeOverride.objects.set_override("heads-down", reason="deep work")
        self._assert_changelist_loads(ModeOverride)

    def test_loop_schedule_changelist_loads(self) -> None:
        schedule = ModeSchedule.objects.create(name="standard", timezone="UTC")
        ModeScheduleSlot.objects.create(schedule=schedule, days=[0, 1, 2], start_time=dt.time(8, 0), preset_name="x")
        self._assert_changelist_loads(ModeSchedule)

    def test_loop_schedule_slot_changelist_loads(self) -> None:
        schedule = ModeSchedule.objects.create(name="standard", timezone="UTC")
        ModeScheduleSlot.objects.create(schedule=schedule, days=[0, 1, 2], start_time=dt.time(8, 0), preset_name="x")
        self._assert_changelist_loads(ModeScheduleSlot)

    def test_loop_schedule_change_form_shows_slot_inline(self) -> None:
        schedule = ModeSchedule.objects.create(name="standard", timezone="UTC")
        ModeScheduleSlot.objects.create(schedule=schedule, days=[0], start_time=dt.time(8, 0), preset_name="engaged")
        url = reverse("admin:core_modeschedule_change", args=[schedule.pk])
        response = self.client.get(url)
        assert response.status_code == 200
        # The inline renders the slot's start_time field on the schedule change form.
        assert b"slots-0-start_time" in response.content
