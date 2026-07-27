"""Django admin registrations for core models.

The autonomous-loop control plane (#1796) is manageable from the Django admin —
``Loop`` rows (name / prompt / delay / enabled) are added, edited, enabled, and
disabled there.
"""

import datetime as dt

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

    def test_config_setting_admin_lists_key_scope_and_a_masked_value(self) -> None:
        model_admin = admin.site._registry[ConfigSetting]
        assert "key" in model_admin.list_display
        assert "scope" in model_admin.list_display
        assert "masked_value" in model_admin.list_display

    def test_the_value_column_is_not_inline_editable(self) -> None:
        """``list_editable`` renders the raw value in a textarea AND writes via ``Model.save()``.

        Both halves of the finding ride on it: the changelist cannot mask a value it
        must round-trip through an input, and the changelist formset bypasses the
        ``set_value`` seam. The change form is the one write surface.
        """
        model_admin = admin.site._registry[ConfigSetting]
        assert "value" not in model_admin.list_editable


class TestConfigSettingAdminSecrecy(django.test.TestCase):
    """A stored secret must not reach the admin's HTML — the same bar the dash holds.

    ``/dash/settings`` masks a secret's value in its table and edits it through a
    write-only input, so the value never enters a response. The admin is the fourth
    config write surface (dash editor, dash import, MCP, admin) and rendered every
    value verbatim.
    """

    #: A recognisable stand-in for a stored secret — its literal absence from the
    #: rendered HTML is the assertion, so it must not collide with any markup.
    SECRET_VALUE = "zzz-stored-secret-marker-zzz"

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser("admin-secrecy", "sec@example.com", "pw")
        self.client.force_login(user)

    def _changelist(self) -> django.http.HttpResponse:
        return self.client.get(reverse("admin:core_configsetting_changelist"))

    def _change_form(self, row: ConfigSetting) -> django.http.HttpResponse:
        return self.client.get(reverse("admin:core_configsetting_change", args=[row.pk]))

    def test_a_secret_value_is_masked_in_the_changelist(self) -> None:
        ConfigSetting.objects.set_value("banned_terms", [self.SECRET_VALUE])
        response = self._changelist()
        assert response.status_code == 200
        assert self.SECRET_VALUE.encode() not in response.content
        assert b"***" in response.content

    def test_a_secret_value_is_not_rendered_on_the_change_form(self) -> None:
        row = ConfigSetting.objects.set_value("banned_terms", [self.SECRET_VALUE])
        response = self._change_form(row)
        assert response.status_code == 200
        assert self.SECRET_VALUE.encode() not in response.content

    def test_a_secret_seed_value_is_not_rendered_on_the_change_form(self) -> None:
        """``seed_value`` is a second copy of the same secret — provenance, not a payload."""
        ConfigSetting.objects.seed("banned_terms", [self.SECRET_VALUE], code_default=[])
        row = ConfigSetting.objects.get(key="banned_terms")
        assert row.seed_value == [self.SECRET_VALUE]
        response = self._change_form(row)
        assert response.status_code == 200
        assert self.SECRET_VALUE.encode() not in response.content

    def test_an_ordinary_value_still_renders_in_the_changelist(self) -> None:
        """Masking is the secret taxonomy, not a blanket blindfold on the changelist."""
        ConfigSetting.objects.set_value("issue_implementer_label", "t3-auto")
        response = self._changelist()
        assert b"t3-auto" in response.content

    def test_a_blank_value_leaves_a_stored_secret_untouched(self) -> None:
        """The write-only input's contract: submitting nothing keeps what is stored."""
        row = ConfigSetting.objects.set_value("banned_terms", [self.SECRET_VALUE])
        url = reverse("admin:core_configsetting_change", args=[row.pk])
        response = self.client.post(url, {"scope": row.scope, "key": row.key, "value": ""})
        assert response.status_code == 302, dict(response.context["adminform"].form.errors)
        row.refresh_from_db()
        assert row.value == [self.SECRET_VALUE]


class TestConfigSettingAdminWritesThroughSetValue(django.test.TestCase):
    """An admin write is an operator write — it runs the ``set_value`` seam, not ``Model.save()``.

    ``ConfigSetting.objects.set_value`` is where the #3688 cross-key consistency
    check and the #3435 seed-provenance clear live. An admin form that called
    ``Model.save()`` could land a coupled pair every other write surface refuses.
    """

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser("admin-seam", "seam@example.com", "pw")
        self.client.force_login(user)

    def _post_change(self, row: ConfigSetting, value: str) -> django.http.HttpResponse:
        url = reverse("admin:core_configsetting_change", args=[row.pk])
        return self.client.post(url, {"scope": row.scope, "key": row.key, "value": value})

    def test_an_inconsistent_cross_key_pair_is_refused_with_a_form_error(self) -> None:
        """``openai_compatible`` is invalid under the default ``claude_sdk`` harness."""
        row = ConfigSetting.objects.set_value("agent_harness_provider", "subscription_oauth")
        response = self._post_change(row, '"openai_compatible"')
        assert response.status_code == 200
        assert response.context["adminform"].form.errors
        row.refresh_from_db()
        assert row.value == "subscription_oauth"

    def test_an_admin_edit_clears_the_seed_provenance(self) -> None:
        """``set_value`` makes the row operator-owned so no redeploy re-seed clobbers it."""
        ConfigSetting.objects.seed("issue_implementer_label", "seeded", code_default="")
        row = ConfigSetting.objects.get(key="issue_implementer_label")
        assert row.seeded_by

        response = self._post_change(row, '"operator-chosen"')

        assert response.status_code == 302
        row.refresh_from_db()
        assert row.value == "operator-chosen"
        assert row.seeded_by == ""
        assert row.seed_value is None


class TestConfigSettingAdminSaves(django.test.TestCase):
    """An empty list/dict is a legitimate override, so the admin must be able to save it.

    ``ConfigSetting`` is a generic key/value store with no per-key arity, and
    ``statusline_chain = []`` means "override the shipped non-empty default with
    nothing". These tests POST through the real admin views — the coverage gap
    that let a blanket non-empty requirement on the storage field ship.

    Every row is seeded NON-empty and emptied by the POST, so the stored-value
    assertion fails on a rejected save. Seeding a row already at the value the
    test then posts leaves ``assert row.value == []`` true whether or not the
    save landed, and only the ``302`` carries any signal.
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
        row = ConfigSetting.objects.set_value("statusline_chain", ["branch", "model"])
        response = self._post_change_form(row, "[]")
        assert response.status_code == 302, self._change_form_errors(response)
        row.refresh_from_db()
        assert row.value == []

    def test_change_form_saves_an_empty_dict_value(self) -> None:
        row = ConfigSetting.objects.set_value("agent_skill_models", {"coder": "opus"})
        response = self._post_change_form(row, "{}")
        assert response.status_code == 302, self._change_form_errors(response)
        row.refresh_from_db()
        assert row.value == {}

    def test_change_form_empties_each_row_the_reported_outage_hit(self) -> None:
        """The three live rows of the reported outage, emptied one change form at a time.

        The outage was a blanket non-empty requirement on the storage field, which
        rejected ``[]`` / ``{}`` for every key. That requirement is per-row, so each
        row proves it independently; the one-formset-for-the-whole-page coupling that
        made it page-wide belonged to ``list_editable``, which no longer exists.
        """
        rows = [
            (ConfigSetting.objects.set_value("statusline_chain", ["branch"]), "[]", []),
            (ConfigSetting.objects.set_value("banned_terms_allowlist", ["acme"]), "[]", []),
            (ConfigSetting.objects.set_value("agent_skill_models", {"coder": "opus"}), "{}", {}),
        ]
        for row, submitted, expected in rows:
            response = self._post_change_form(row, submitted)
            assert response.status_code == 302, self._change_form_errors(response)
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
