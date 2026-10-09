import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar, override

from django import forms
from django.apps import apps
from django.contrib import admin
from django.db import models
from django.db.models.constants import LOOKUP_SEP
from django.forms.renderers import BaseRenderer
from django.utils.safestring import SafeString

from teatree.config.credential_pass_key import validate_pass_key_entry
from teatree.config.write_validation import validate_config_write
from teatree.core.config_display import is_secret, masked_display, withholds_value
from teatree.core.models import (
    ConfigSetting,
    Loop,
    LoopState,
    Mode,
    ModeOverride,
    ModeSchedule,
    ModeScheduleSlot,
    Prompt,
    PromptVersion,
    Ticket,
)
from teatree.core.models.config_setting import ConfigValue
from teatree.core.overlays.overlay_credentials import known_pass_key_credential

if TYPE_CHECKING:
    from django.http import HttpRequest

# Reads as a credential, a command or captured text; Text/JSON/Binary columns are hidden by type already.
_MASKED_NAME = re.compile(
    r"token|secret|passw|credential|cookie|webhook|api_?key|private_?key|command|cmd|argv"
    r"|(^|_)(env|pass|value|body|text|message|payload|prompt|content|output|stdout|stderr"
    r"|error|reason|rationale|question|signature|detail|note|hint|email)(_|$)",
    re.IGNORECASE,
)
_NOT_TEXTUAL = (
    models.IntegerField,
    models.FloatField,
    models.DecimalField,
    models.DateField,
    models.TimeField,
    models.DurationField,
    models.BooleanField,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    show_full_result_count = False
    list_display_links = None

    @staticmethod
    def is_masked(field: models.Field) -> bool:
        if isinstance(field, models.TextField | models.JSONField | models.BinaryField):
            return True
        if field.is_relation or field.choices or isinstance(field, _NOT_TEXTUAL):
            return False
        return _MASKED_NAME.search(field.name) is not None

    def visible_fields(self) -> list[models.Field]:
        return [field for field in self.opts.concrete_fields if not self.is_masked(field)]

    @override
    def get_list_display(self, request: "HttpRequest") -> list[str]:
        return [field.attname for field in self.visible_fields()]

    @override
    def get_list_filter(self, request: "HttpRequest") -> list[str]:
        return [
            field.name for field in self.visible_fields() if field.choices or isinstance(field, models.BooleanField)
        ]

    @override
    def get_queryset(self, request: "HttpRequest") -> models.QuerySet:
        return super().get_queryset(request).only(*(field.attname for field in self.visible_fields()))

    # django-types 0.24 predates the ``request`` argument Django 5.0 added to this hook.
    @override
    def lookup_allowed(self, lookup: str, value: str, request: "HttpRequest") -> bool:  # ty: ignore[invalid-method-override]
        masked = {field.name for field in self.opts.concrete_fields if self.is_masked(field)}
        allowed = super().lookup_allowed(lookup, value, request)  # ty: ignore[too-many-positional-arguments]
        return lookup.split(LOOKUP_SEP, 1)[0] not in masked and allowed

    @override
    def has_add_permission(self, request: "HttpRequest") -> bool:
        return False

    @override
    def has_change_permission(self, request: "HttpRequest", obj: models.Model | None = None) -> bool:
        return False

    @override
    def has_delete_permission(self, request: "HttpRequest", obj: models.Model | None = None) -> bool:
        return False

    @override
    def has_view_permission(self, request: "HttpRequest", obj: models.Model | None = None) -> bool:
        return obj is None and super().has_view_permission(request)


def register_read_only_defaults(site: admin.AdminSite, candidates: Iterable[type[models.Model]]) -> None:
    for model in candidates:
        if site.is_registered(model) or isinstance(ReadOnlyAdmin(model, site).opts.pk, models.CompositePrimaryKey):
            continue
        site.register(model, ReadOnlyAdmin)


@admin.register(Ticket)
class TicketAdmin(ReadOnlyAdmin):
    search_fields = ("issue_url", "repo_namespaced_key")
    ordering = ("-pk",)


class ShippedDeleteRouting(admin.ModelAdmin):
    """Route deleting a SHIPPED row to the CLI seam that carries the typed confirm (#3842).

    Not a prohibition — the admin's own button has nowhere to type a phrase and records
    nothing, so a shipped row is deleted through ``t3 loops delete`` / ``t3 loop preset
    delete`` / ``t3 loop schedule delete``, each of which names what stops happening first.
    An operator-created row keeps the ordinary admin delete.
    """

    shipped_family: str = ""

    def has_delete_permission(self, request: "HttpRequest", obj: object = None) -> bool:
        from teatree.config.seed_defaults import is_shipped  # noqa: PLC0415 — deferred: reads the packaged seed file

        if not super().has_delete_permission(request, obj):
            return False
        if obj is None:
            return True
        return not is_shipped(self.shipped_family, str(getattr(obj, "name", "")))


@admin.register(Loop)
class LoopAdmin(ShippedDeleteRouting):
    shipped_family = "loop"
    list_display = (
        "name",
        "enabled",
        "override_reason",
        "override_expected_lift_at",
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


@admin.register(LoopState)
class LoopStateAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("name",)


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
        """Run the key's registry parser like every write surface; a blank write-only submission keeps a secret."""
        value = self.cleaned_data.get("value")
        if value is None and self._edits_a_secret():
            return self.instance.value
        key = self.cleaned_data.get("key", "")
        if not key:
            return value
        try:
            return (
                validate_pass_key_entry(value) if known_pass_key_credential(key) else validate_config_write(key, value)
            )
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


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
        """Give a withheld value a write-only ``value`` widget so it never renders."""
        if obj is not None and withholds_value(obj.key, obj.value):
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
class ModeAdmin(ShippedDeleteRouting):
    shipped_family = "preset"
    list_display = ("name", "entry_count", "egress", "description", "updated_at")
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(ModeOverride)
class ModeOverrideAdmin(admin.ModelAdmin):
    list_display = ("preset_name", "reason", "expected_lift_at", "set_at")
    search_fields = ("preset_name",)
    readonly_fields = ("set_at",)


class ModeScheduleSlotInline(admin.TabularInline):
    """Edit a schedule's slots (days / start time / preset) in place under it (#3159, LP-4)."""

    model = ModeScheduleSlot
    extra = 1
    fields = ("days", "start_time", "preset_name")


@admin.register(ModeSchedule)
class ModeScheduleAdmin(ShippedDeleteRouting):
    shipped_family = "schedule"
    list_display = ("name", "timezone", "description", "updated_at")
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (ModeScheduleSlotInline,)


@admin.register(ModeScheduleSlot)
class ModeScheduleSlotAdmin(admin.ModelAdmin):
    list_display = ("id", "schedule", "days", "start_time", "preset_name")
    list_filter = ("schedule",)
    search_fields = ("preset_name",)


# Last on purpose: every core model without a hand-written admin above gets the read-only default.
register_read_only_defaults(admin.site, apps.get_app_config("core").get_models())
