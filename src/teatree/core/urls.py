from django.urls import path

from teatree.core.views.actions import CancelTaskView, CreateTaskView, SyncFollowupView, TicketTransitionView
from teatree.core.views.dashboard import (
    DashboardPanelView,
    DashboardView,
    TaskDetailView,
    TaskGraphView,
    TicketLifecycleView,
)
from teatree.core.views.history import SessionHistoryView
from teatree.core.views.launch import LaunchAgentView, LaunchInteractiveAgentView, LaunchTerminalView
from teatree.core.views.sse import DashboardSSEView

app_name = "teatree"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("dashboard/panels/<str:panel>/", DashboardPanelView.as_view(), name="dashboard-panel"),
    path("dashboard/events/", DashboardSSEView.as_view(), name="dashboard-events"),
    path("dashboard/sync/", SyncFollowupView.as_view(), name="dashboard-sync"),
    path("tasks/<int:task_id>/detail/", TaskDetailView.as_view(), name="task-detail"),
    path("tickets/<int:ticket_id>/task-graph/", TaskGraphView.as_view(), name="task-graph"),
    path("tickets/<int:ticket_id>/lifecycle/", TicketLifecycleView.as_view(), name="ticket-lifecycle"),
    path("tasks/<int:task_id>/launch/", LaunchAgentView.as_view(), name="task-launch"),
    path("tasks/<int:task_id>/cancel/", CancelTaskView.as_view(), name="task-cancel"),
    path("tickets/<int:ticket_id>/transition/", TicketTransitionView.as_view(), name="ticket-transition"),
    path("tickets/<int:ticket_id>/create-task/", CreateTaskView.as_view(), name="ticket-create-task"),
    path("dashboard/launch-terminal/", LaunchTerminalView.as_view(), name="launch-terminal"),
    path("dashboard/launch-agent/", LaunchInteractiveAgentView.as_view(), name="launch-interactive-agent"),
    path("sessions/<str:session_id>/history/", SessionHistoryView.as_view(), name="session-history"),
]
