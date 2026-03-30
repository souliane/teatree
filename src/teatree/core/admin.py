from django.contrib import admin

from teatree.core.models import Session, Task, TaskAttempt, Ticket, Worktree


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
