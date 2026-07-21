from django.contrib import admin

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


@admin.register(ConfigSetting)
class ConfigSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "scope", "value", "updated_at")
    list_editable = ("value",)
    list_filter = ("scope",)
    search_fields = ("key", "scope")
    readonly_fields = ("created_at", "updated_at")


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
