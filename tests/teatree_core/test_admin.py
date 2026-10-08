"""Django admin registrations for core models.

The autonomous-loop control plane (#1796) is manageable from the Django admin —
``Loop`` rows (name / prompt / delay / enabled) are added, edited, enabled, and
disabled there.
"""

import datetime as dt

import django.http
import django.test
from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import connection, models
from django.template.loader import get_template
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from teatree.core.admin import ReadOnlyAdmin
from teatree.core.models import (
    ConfigSetting,
    DeferredQuestion,
    DeferredQuestionAudit,
    Loop,
    LoopState,
    LoopStatus,
    Mode,
    ModeOverride,
    ModeSchedule,
    ModeScheduleSlot,
    OnBehalfApproval,
    OnBehalfAudit,
    Prompt,
    SelfImproveFiring,
    SelfUpdateMarker,
    SessionTodo,
    StandingGoal,
    Task,
    Ticket,
    TicketTransition,
    TrustedIdentity,
    WorktreeEnvOverride,
)
from tests.factories import (
    MergeAuditFactory,
    SessionFactory,
    TaskFactory,
    TicketFactory,
    TicketTransitionFactory,
    WorktreeFactory,
)


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

    def _secret_registry(self) -> dict[str, list[str]]:
        return {"leak": [self.SECRET_VALUE], "prose_collider": []}

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser("admin-secrecy", "sec@example.com", "pw")
        self.client.force_login(user)

    def _changelist(self) -> django.http.HttpResponse:
        return self.client.get(reverse("admin:core_configsetting_changelist"))

    def _change_form(self, row: ConfigSetting) -> django.http.HttpResponse:
        return self.client.get(reverse("admin:core_configsetting_change", args=[row.pk]))

    def test_a_secret_value_is_masked_in_the_changelist(self) -> None:
        ConfigSetting.objects.set_value("banned_term_registry", self._secret_registry())
        response = self._changelist()
        assert response.status_code == 200
        assert self.SECRET_VALUE.encode() not in response.content
        assert b"***" in response.content

    def test_a_secret_value_is_not_rendered_on_the_change_form(self) -> None:
        row = ConfigSetting.objects.set_value("banned_term_registry", self._secret_registry())
        response = self._change_form(row)
        assert response.status_code == 200
        assert self.SECRET_VALUE.encode() not in response.content

    def test_a_secret_seed_value_is_not_rendered_on_the_change_form(self) -> None:
        """``seed_value`` is a second copy of the same secret — provenance, not a payload."""
        ConfigSetting.objects.seed("banned_term_registry", self._secret_registry(), code_default={})
        row = ConfigSetting.objects.get(key="banned_term_registry")
        assert row.seed_value == self._secret_registry()
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
        row = ConfigSetting.objects.set_value("banned_term_registry", self._secret_registry())
        url = reverse("admin:core_configsetting_change", args=[row.pk])
        response = self.client.post(url, {"scope": row.scope, "key": row.key, "value": ""})
        assert response.status_code == 302, dict(response.context["adminform"].form.errors)
        row.refresh_from_db()
        assert row.value == self._secret_registry()


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

    def test_add_form_refuses_an_unknown_setting_key(self) -> None:
        response = self.client.post(
            reverse("admin:core_configsetting_add"),
            {"scope": "", "key": "unregistered_setting", "value": "1"},
        )
        assert response.status_code == 200
        assert "unknown config key" in str(response.context["adminform"].form.errors)
        assert not ConfigSetting.objects.filter(key="unregistered_setting").exists()

    def test_add_form_accepts_a_known_pass_key_setting(self) -> None:
        response = self.client.post(
            reverse("admin:core_configsetting_add"),
            {"scope": "", "key": "github_token_pass_key", "value": '"team/github/token"'},
        )
        assert response.status_code == 302, self._change_form_errors(response)
        assert ConfigSetting.objects.get(scope="", key="github_token_pass_key").value == "team/github/token"

    def test_change_form_saves_an_empty_dict_value(self) -> None:
        row = ConfigSetting.objects.set_value("agent_skill_models", {"coder": [{"floor": "opus"}]})
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
            (ConfigSetting.objects.set_value("disk_cache_allowlist", ["acme"]), "[]", []),
            (ConfigSetting.objects.set_value("agent_skill_models", {"coder": [{"floor": "opus"}]}), "{}", {}),
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
        Mode.objects.create(name="maintenance", entries={"review": False})
        self._assert_changelist_loads(Mode)

    def test_loop_preset_override_changelist_loads(self) -> None:
        ModeOverride.objects.set_override("maintenance", reason="deep work")
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
        ModeScheduleSlot.objects.create(schedule=schedule, days=[0], start_time=dt.time(8, 0), preset_name="present")
        url = reverse("admin:core_modeschedule_change", args=[schedule.pk])
        response = self.client.get(url)
        assert response.status_code == 200
        # The inline renders the slot's start_time field on the schedule change form.
        assert b"slots-0-start_time" in response.content


class TestShippedRowsRouteDeletionToTheAuditedSeam(django.test.TestCase):
    """The admin button carries no phrase and records nothing, so a shipped row uses the CLI (#3842).

    Routing, not prohibition — `t3 loops delete` / `t3 loop preset delete` /
    `t3 loop schedule delete` each take the typed `stop-<name>`. An operator-created row
    keeps the ordinary admin delete, so the friction lands only where an accidental
    deletion silently stops work the box shipped configured to do.
    """

    def _request(self) -> django.http.HttpRequest:
        """A superuser request — the override defers to Django's own permission check first."""
        request = django.http.HttpRequest()
        request.user = get_user_model().objects.create_superuser(
            f"admin-shipped-{self.id().rsplit('.', 1)[-1]}", "shipped@example.com", "pw"
        )
        return request

    def test_a_shipped_loop_cannot_be_deleted_from_the_admin(self) -> None:
        loop, _ = Loop.objects.get_or_create(
            name="review", defaults={"script": "src/teatree/loops/review/loop.py", "delay_seconds": 300}
        )

        assert admin.site._registry[Loop].has_delete_permission(self._request(), loop) is False

    def test_an_operator_created_loop_keeps_the_admin_delete(self) -> None:
        loop = Loop.objects.create(name="operator-custom", script="src/teatree/loops/x/loop.py", delay_seconds=300)

        assert admin.site._registry[Loop].has_delete_permission(self._request(), loop) is True

    def test_the_changelist_still_renders_when_no_object_is_bound(self) -> None:
        """``obj=None`` is the changelist's own probe — denying it would break the whole page."""
        assert admin.site._registry[Loop].has_delete_permission(self._request(), None) is True

    def test_a_non_superuser_is_still_denied_by_djangos_own_check(self) -> None:
        """The override narrows Django's permission, never widens it."""
        request = django.http.HttpRequest()
        request.user = get_user_model().objects.create_user("admin-plain", "plain@example.com", "pw")
        loop = Loop.objects.create(name="operator-custom", script="src/teatree/loops/x/loop.py", delay_seconds=300)

        assert admin.site._registry[Loop].has_delete_permission(request, loop) is False

    def test_a_shipped_preset_and_schedule_route_the_same_way(self) -> None:
        preset, _ = Mode.objects.get_or_create(name="present", defaults={"entries": {}, "description": "shipped"})
        schedule, _ = ModeSchedule.objects.get_or_create(name="standard", defaults={"description": "shipped"})
        request = self._request()

        assert admin.site._registry[Mode].has_delete_permission(request, preset) is False
        assert admin.site._registry[ModeSchedule].has_delete_permission(request, schedule) is False

    def test_a_loop_name_does_not_make_a_preset_shipped(self) -> None:
        """Families are scoped to their own seed table — `review` ships as a loop, not a preset."""
        preset = Mode.objects.create(name="review", entries={}, description="operator-created")  # a LOOP name

        assert admin.site._registry[Mode].has_delete_permission(self._request(), preset) is True


_SENTINEL = "zz-hidden-sentinel-zz"
_WRITE_STATEMENTS = ("INSERT", "UPDATE", "DELETE", "BEGIN", "SAVEPOINT")


def _read_only_models() -> list[type[models.Model]]:
    return [
        model
        for model in apps.get_app_config("core").get_models()
        if isinstance(admin.site.get_model_admin(model), ReadOnlyAdmin)
    ]


def _admin_url(model: type[models.Model], view: str, *args: object) -> str:
    return reverse(f"admin:core_{model._meta.model_name}_{view}", args=args)


def _row_snapshot(row: models.Model) -> dict[str, object]:
    return type(row).objects.filter(pk=row.pk).values().get()


def _statements(capture: CaptureQueriesContext, *prefixes: str) -> list[str]:
    return [query["sql"] for query in capture.captured_queries if query["sql"].lstrip().upper().startswith(prefixes)]


class _SuperuserTestCase(django.test.TestCase):
    def setUp(self) -> None:
        self.superuser = get_user_model().objects.create_superuser("admin-read-only", "ro@example.com", "pw")
        self.client.force_login(self.superuser)


class TestReadOnlyAdminRefusesWrites(_SuperuserTestCase):
    """Factory rows, approvals and ledgers are written by their own seams; an admin write forges them."""

    @staticmethod
    def _guarded_rows() -> list[models.Model]:
        approval = OnBehalfApproval.objects.create(target="pr:1", action="approve", approver_id="owner")
        audit = MergeAuditFactory.create()
        return [
            approval,
            TrustedIdentity.objects.create(platform=TrustedIdentity.Platform.GITHUB, handle="owner"),
            audit.clear,
            OnBehalfAudit.objects.create(approval=approval, target="pr:1", action="approve", approver_id="owner"),
            audit,
            TicketTransitionFactory.create(),
            TicketFactory.create(),
            TaskFactory.create(),
        ]

    def test_add_change_delete_history_and_detail_answer_403(self) -> None:
        for row in self._guarded_rows():
            model = type(row)
            before = _row_snapshot(row)
            with self.subTest(model=model.__name__):
                statuses = {
                    "add": self.client.get(_admin_url(model, "add")).status_code,
                    "add POST": self.client.post(_admin_url(model, "add"), {}).status_code,
                    "detail": self.client.get(_admin_url(model, "change", row.pk)).status_code,
                    "change POST": self.client.post(_admin_url(model, "change", row.pk), {}).status_code,
                    "delete": self.client.get(_admin_url(model, "delete", row.pk)).status_code,
                    "delete POST": self.client.post(_admin_url(model, "delete", row.pk), {"post": "yes"}).status_code,
                    "history": self.client.get(_admin_url(model, "history", row.pk)).status_code,
                }
                assert statuses == dict.fromkeys(statuses, 403)
                assert _row_snapshot(row) == before

    def test_every_read_only_model_refuses_an_add(self) -> None:
        models_under_test = _read_only_models()
        assert len(models_under_test) > 90
        for model in models_under_test:
            with self.subTest(model=model.__name__):
                assert self.client.post(_admin_url(model, "add"), {}).status_code == 403

    def test_bulk_delete_action_deletes_nothing(self) -> None:
        for row in (TicketTransitionFactory(), OnBehalfApproval.objects.create(target="pr:2", approver_id="owner")):
            model = type(row)
            with self.subTest(model=model.__name__):
                self.client.post(
                    _admin_url(model, "changelist"),
                    {"action": "delete_selected", "_selected_action": [row.pk], "index": 0, "post": "yes"},
                )
                assert model.objects.filter(pk=row.pk).exists()

    def test_a_state_post_leaves_the_state(self) -> None:
        ticket = TicketFactory(state=Ticket.State.REVIEW_REQUESTED)
        task = TaskFactory(status=Task.Status.PENDING)
        for row, field, forged in ((ticket, "state", Ticket.State.MERGED), (task, "status", Task.Status.COMPLETED)):
            before = getattr(row, field)
            with self.subTest(model=type(row).__name__):
                response = self.client.post(_admin_url(type(row), "change", row.pk), {field: forged})
                row.refresh_from_db()
                assert (response.status_code, getattr(row, field)) == (403, before)


class TestReadOnlyChangelistNeverRendersSensitiveFields(_SuperuserTestCase):
    def test_sentinels_in_hidden_columns_never_reach_the_html(self) -> None:
        rows = [
            SessionTodo.objects.create(session=SessionFactory(), text=_SENTINEL),
            WorktreeEnvOverride.objects.create(worktree=WorktreeFactory(), key="VISIBLE_KEY", value=_SENTINEL),
            StandingGoal.objects.create(name="visible-goal", check_command=_SENTINEL),
            SelfUpdateMarker.objects.create(repo_label="visible-repo", last_reason=_SENTINEL),
        ]
        for row in rows:
            with self.subTest(model=type(row).__name__):
                response = self.client.get(_admin_url(type(row), "changelist"))
                assert (response.status_code, response.context["cl"].result_count) == (200, 1)
                assert _SENTINEL not in response.content.decode()

    def test_a_foreign_key_renders_its_id_not_the_related_text(self) -> None:
        question = DeferredQuestion.objects.create(question=f"{_SENTINEL} would you merge?")
        assert _SENTINEL in str(question)
        DeferredQuestionAudit.objects.create(question=question, action="answered")

        content = self.client.get(_admin_url(DeferredQuestionAudit, "changelist")).content.decode()

        assert f'<td class="field-question_id">{question.pk}</td>' in content
        assert _SENTINEL not in content

    def test_no_changelist_selects_a_hidden_column(self) -> None:
        for model in _read_only_models():
            model_admin = admin.site.get_model_admin(model)
            request = django.test.RequestFactory().get(_admin_url(model, "changelist"))
            request.user = self.superuser
            select_list = str(model_admin.get_changelist_instance(request).queryset.query).split(" FROM ", 1)[0]
            table = model._meta.db_table
            with self.subTest(model=model.__name__):
                assert f'"{table}"."{model._meta.pk.column}"' in select_list
                selected = [
                    field.name
                    for field in model._meta.concrete_fields
                    if model_admin.is_masked(field) and f'"{table}"."{field.column}"' in select_list
                ]
                assert selected == []


class TestReadOnlyChangelistLookups(_SuperuserTestCase):
    def test_a_lookup_on_a_hidden_column_answers_400(self) -> None:
        WorktreeEnvOverride.objects.create(worktree=WorktreeFactory(), key="KEY", value=_SENTINEL)
        for model, query in (
            (WorktreeEnvOverride, {"value__startswith": "x"}),
            (SessionTodo, {"text__icontains": "x"}),
            (SelfImproveFiring, {"payload__a": "1"}),
        ):
            with self.subTest(model=model.__name__):
                assert self.client.get(_admin_url(model, "changelist"), query).status_code == 400

    def test_a_lookup_on_a_visible_column_still_filters(self) -> None:
        worktree = WorktreeFactory()
        WorktreeEnvOverride.objects.create(worktree=worktree, key="XA_KEY", value="1")
        WorktreeEnvOverride.objects.create(worktree=worktree, key="YB_KEY", value="2")

        response = self.client.get(_admin_url(WorktreeEnvOverride, "changelist"), {"key__startswith": "XA"})

        assert (response.status_code, response.context["cl"].result_count) == (200, 1)
        assert "XA_KEY" in response.content.decode()
        assert "YB_KEY" not in response.content.decode()


class TestReadOnlyChangelistCost(_SuperuserTestCase):
    def _changelist_statements(self, model: type[models.Model]) -> list[str]:
        with CaptureQueriesContext(connection) as capture:
            assert self.client.get(_admin_url(model, "changelist")).status_code == 200
        return [query["sql"] for query in capture.captured_queries]

    def test_one_count_and_no_write_statement(self) -> None:
        TicketTransitionFactory()
        with CaptureQueriesContext(connection) as denied:
            assert self.client.post(_admin_url(TicketTransition, "add"), {}).status_code == 403
        assert _statements(denied, *_WRITE_STATEMENTS), "the capture must see a denied write's SAVEPOINT"

        with CaptureQueriesContext(connection) as changelist:
            assert self.client.get(_admin_url(TicketTransition, "changelist")).status_code == 200

        counts = [query["sql"] for query in changelist.captured_queries if "COUNT(" in query["sql"].upper()]
        assert len(counts) == 1
        assert _statements(changelist, *_WRITE_STATEMENTS) == []

    def test_statement_count_is_independent_of_row_count(self) -> None:
        def add_rows(count: int) -> None:
            for index in range(count):
                WorktreeEnvOverride.objects.create(worktree=WorktreeFactory(), key=f"KEY_{index}", value="v")

        add_rows(3)
        self._changelist_statements(WorktreeEnvOverride)
        at_three = len(self._changelist_statements(WorktreeEnvOverride))
        add_rows(27)

        assert len(self._changelist_statements(WorktreeEnvOverride)) == at_three

    def test_every_read_only_changelist_renders(self) -> None:
        for model in _read_only_models():
            with self.subTest(model=model.__name__):
                assert self.client.get(_admin_url(model, "changelist")).status_code == 200


class TestTicketAutocomplete(_SuperuserTestCase):
    def test_the_autocomplete_view_serves_the_issue_url_and_never_hidden_text(self) -> None:
        ticket = TicketFactory(issue_url="https://github.com/souliane/teatree/issues/987654", context=_SENTINEL)

        response = self.client.get(
            reverse("admin:autocomplete"),
            {"app_label": "core", "model_name": "session", "field_name": "ticket", "term": "987654"},
        )

        assert response.status_code == 200
        assert [result["text"] for result in response.json()["results"]] == [ticket.issue_url]
        assert _SENTINEL not in response.content.decode()


class TestLoopStateAdmin(_SuperuserTestCase):
    def test_the_dash_break_glass_link_resolves_and_a_hold_can_be_lifted(self) -> None:
        link = "/admin/core/loopstate/"
        assert link in get_template("dash/partials/_loops_table.html").template.source
        hold = LoopState.objects.pause("review")

        assert self.client.get(link).status_code == 200
        response = self.client.post(f"{link}{hold.pk}/change/", {"name": hold.name, "status": LoopStatus.ENABLED})

        assert response.status_code == 302
        assert LoopState.objects.is_runnable("review")
