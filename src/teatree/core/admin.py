from django.contrib import admin

from teatree.core.models import Loop, PullRequest, Session, Task, TaskAttempt, Ticket, Worktree


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id", "state", "variant", "issue_url")


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
        "action",
        "run_in_sub_agent",
        "description",
        "cadence",
        "last_run_at",
        "updated_at",
    )
    list_editable = ("enabled",)
    search_fields = ("name",)
    readonly_fields = ("last_run_at", "created_at", "updated_at")

    @admin.display(description="action")
    @staticmethod
    def action(obj: Loop) -> str:
        return obj.script or obj.prompt

    @admin.display(description="cadence")
    @staticmethod
    def cadence(obj: Loop) -> str:
        return obj.cadence_label
