from typing import TYPE_CHECKING, Any, ClassVar, override

from django import forms
from django.contrib import admin
from django.forms.renderers import BaseRenderer
from django.utils.safestring import SafeString

from teatree.core.config_display import is_secret, masked_display
from teatree.core.models import (
    ConfigSetting,
    Loop,
    Mode,
    ModeOverride,
    ModeSchedule,
    ModeScheduleSlot,
    Prompt,
    PromptVersion,
    PullRequest,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    Worktree,
)
from teatree.core.models.config_setting import ConfigValue

if TYPE_CHECKING:
    from django.http import HttpRequest


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "state", "variant", "issue_url", "repo_namespaced_key")
    search_fields = ("issue_url", "repo_namespaced_key")


@admin.register(Worktree)
class WorktreeAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "repo_path", "branch", "state")


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "agent_id", "started_at", "ended_at")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "execution_target", "status", "claimed_by")


@admin.register(TaskAttempt)
class TaskAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "execution_target", "exit_code", "ended_at")


@admin.register(PullRequest)
class PullRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "repo", "iid", "state")


@admin.register(Loop)
class LoopAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "enabled",
        "colleague_facing",
        "action",
        "run_in_sub_agent",
        "description",
        "cadence",
        "last_run_at",
        "updated_at",
    )
    list_editable = ("enabled", "colleague_facing")
    search_fields = ("name",)
    readonly_fields = ("last_run_at", "created_at", "updated_at")

    @admin.display(description="action")
    @staticmethod
    def action(obj: Loop) -> str:
        """The loop's invocation: its ``script`` path, or its prompt's body (#2513)."""
        return obj.script or (obj.prompt.body if obj.prompt_id is not None else "")  # ty: ignore[unresolved-attribute]

    @admin.display(description="cadence")
    @staticmethod
    def cadence(obj: Loop) -> str:
        return obj.cadence_label


class PromptVersionInline(admin.TabularInline):
    """Read-only superseded-content history under each prompt (#2513, D2)."""

    model = PromptVersion
    extra = 0
    fields = ("version", "body", "params", "created_at")
    readonly_fields = ("version", "body", "params", "created_at")
    can_delete = False
    ordering = ("-version",)


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = ("name", "overlay", "current_version", "description", "updated_at")
    search_fields = ("name", "overlay")
    readonly_fields = ("created_at", "updated_at")
    inlines = (PromptVersionInline,)

    @admin.display(description="versions")
    @staticmethod
    def current_version(obj: Prompt) -> int:
        return obj.current_version


_WRITE_ONLY_ATTRS = {"rows": 4, "cols": 40, "placeholder": "write-only — leave blank to keep the stored value"}


class WriteOnlyJSONWidget(forms.Textarea):
    """A textarea that never renders the value it holds — a secret leaves no HTML trace.

    The admin's counterpart to the settings editor's write-only password input: the
    operator can SET a secret from the change form, and submitting nothing keeps what
    is stored (``ConfigSettingAdminForm.clean_value``).
    """

    @override
    def render(
        self,
        name: str,
        value: object,
        attrs: dict[str, Any] | None = None,
        renderer: BaseRenderer | None = None,
    ) -> SafeString:
        return super().render(name, "", attrs, renderer)


class ConfigSettingAdminForm(forms.ModelForm):
    """Edits a setting the way every other write surface does — masked, and through the seam.

    ``value`` is write-only for a secret key, so the change form is not a second place
    a stored secret renders in cleartext. Cross-key consistency (#3688) is enforced by
    ``ConfigSetting.clean``, which every ``ModelForm`` runs, so an inconsistent coupled
    pair surfaces as a field error rather than landing silently.
    """

    class Meta:
        model = ConfigSetting
        fields: ClassVar = ["scope", "key", "value"]

    def _edits_a_secret(self) -> bool:
        return bool(self.instance.pk) and is_secret(self.instance.key)

    def clean_value(self) -> ConfigValue | None:
        """A blank write-only submission means "leave the stored secret alone"."""
        value = self.cleaned_data.get("value")
        if value is None and self._edits_a_secret():
            return self.instance.value
        return value


@admin.register(ConfigSetting)
class ConfigSettingAdmin(admin.ModelAdmin):
    """The admin config surface, held to the same bar as ``/dash/settings`` (#3760 follow-up).

    Two properties the dash editor has always had and this surface lacked. A secret's
    value is MASKED wherever it would render, and every write runs
    ``ConfigSetting.objects.set_value`` — the seam carrying the #3688 cross-key check
    and the #3435 seed-provenance clear. ``list_editable`` is deliberately absent: an
    inline widget must round-trip the raw value (so it cannot mask) and the changelist
    formset writes through ``Model.save()`` (so it cannot use the seam).
    """

    form = ConfigSettingAdminForm
    list_display = ("key", "scope", "masked_value", "updated_at")
    list_filter = ("scope",)
    search_fields = ("key", "scope")
    fields = ("scope", "key", "value", "seeded_by", "masked_seed_value", "created_at", "updated_at")
    readonly_fields = ("seeded_by", "masked_seed_value", "created_at", "updated_at")

    @override
    def get_form(
        self, request: "HttpRequest", obj: ConfigSetting | None = None, change: bool = False, **kwargs: object
    ) -> type[forms.ModelForm]:
        """Give a secret key a write-only ``value`` widget so its stored value never renders."""
        if obj is not None and is_secret(obj.key):
            kwargs["widgets"] = {"value": WriteOnlyJSONWidget(attrs=_WRITE_ONLY_ATTRS)}
        return super().get_form(request, obj, change, **kwargs)

    @admin.display(description="value")
    @staticmethod
    def masked_value(obj: ConfigSetting) -> str:
        return masked_display(obj.key, obj.value)

    @admin.display(description="seed value")
    @staticmethod
    def masked_seed_value(obj: ConfigSetting) -> str:
        """Seed provenance carries a second copy of the same value — mask it identically."""
        return masked_display(obj.key, obj.seed_value)

    @override
    def save_model(self, request: "HttpRequest", obj: ConfigSetting, form: forms.ModelForm, change: bool) -> None:
        """Write through ``set_value``, and drop the row the edit moved off its old key."""
        if change:
            previous = ConfigSetting.objects.filter(pk=obj.pk).first()
            if previous is not None and (previous.scope, previous.key) != (obj.scope, obj.key):
                ConfigSetting.objects.clear(previous.key, scope=previous.scope)
        ConfigSetting.objects.set_value(obj.key, obj.value, scope=obj.scope)


@admin.register(Mode)
class ModeAdmin(admin.ModelAdmin):
    list_display = ("name", "availability_mode", "entry_count", "description", "updated_at")
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(ModeOverride)
class ModeOverrideAdmin(admin.ModelAdmin):
    list_display = ("preset_name", "until", "reason", "set_at")
    search_fields = ("preset_name",)
    readonly_fields = ("set_at",)


class ModeScheduleSlotInline(admin.TabularInline):
    """Edit a schedule's slots (days / start time / preset) in place under it (#3159, LP-4)."""

    model = ModeScheduleSlot
    extra = 1
    fields = ("days", "start_time", "preset_name")


@admin.register(ModeSchedule)
class ModeScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "timezone", "description", "updated_at")
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (ModeScheduleSlotInline,)


@admin.register(ModeScheduleSlot)
class ModeScheduleSlotAdmin(admin.ModelAdmin):
    list_display = ("id", "schedule", "days", "start_time", "preset_name")
    list_filter = ("schedule",)
    search_fields = ("preset_name",)
