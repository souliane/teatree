"""URL config for the ``teatree.dash`` admin dashboard app (#3162).

Mounted at ``/dash/`` by the project URLconf. Full-page GETs render the pages
(board / health / loops / presets / settings); the ``*_partial`` and ``readouts``
routes serve the htmx-poll fragments; the POST routes are the CSRF-protected
mutations.
"""

from django.urls import path
from django.views.generic.base import RedirectView

from teatree.dash.views import (
    board,
    board_columns_partial,
    command_run,
    cycle_time,
    debug_session,
    gate_toggle,
    health,
    health_bands_partial,
    live,
    live_body_partial,
    loop_action,
    loop_cadence,
    loops,
    loops_table_partial,
    posture,
    preset_create,
    preset_delete,
    preset_entry,
    preset_meta,
    preset_rename,
    preset_use,
    presets,
    runner_toggle,
    schedule_activate,
    schedule_slot,
    schedule_slot_delete,
    sessions,
    settings,
    settings_export,
    settings_group,
    settings_import,
    settings_readouts,
    settings_restore,
    settings_set,
    ticket_drawer,
    ticket_transition,
    transcript,
)

app_name = "dash"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="dash:board", permanent=False), name="index"),
    path("board/", board, name="board"),
    path("board/columns/", board_columns_partial, name="board_columns"),
    path("cycle-time/", cycle_time, name="cycle_time"),
    path("health/", health, name="health"),
    path("health/bands/", health_bands_partial, name="health_bands"),
    # Retired: the config page merged into /dash/settings/. Kept as a redirect so an old
    # bookmark, doc link or skill reference lands on the page that absorbed it.
    path("config/", RedirectView.as_view(pattern_name="dash:settings", permanent=False), name="config"),
    path("live/", live, name="live"),
    path("live/body/", live_body_partial, name="live_body"),
    path("loops/", loops, name="loops"),
    path("loops/table/", loops_table_partial, name="loops_table"),
    path("loops/action/", loop_action, name="loop_action"),
    path("loops/posture/", posture, name="posture"),
    path("loops/gate/", gate_toggle, name="gate_toggle"),
    path("loops/runner/", runner_toggle, name="runner_toggle"),
    path("loops/cadence/", loop_cadence, name="loop_cadence"),
    path("presets/", presets, name="presets"),
    path("presets/entry/", preset_entry, name="preset_entry"),
    path("presets/use/", preset_use, name="preset_use"),
    path("presets/create/", preset_create, name="preset_create"),
    path("presets/meta/", preset_meta, name="preset_meta"),
    path("presets/rename/", preset_rename, name="preset_rename"),
    path("presets/delete/", preset_delete, name="preset_delete"),
    path("presets/schedule/", schedule_activate, name="schedule_activate"),
    path("presets/schedule/slot/", schedule_slot, name="schedule_slot"),
    path("presets/schedule/slot/delete/", schedule_slot_delete, name="schedule_slot_delete"),
    path("sessions/", sessions, name="sessions"),
    path("settings/", settings, name="settings"),
    path("settings/readouts/", settings_readouts, name="settings_readouts"),
    path("settings/group/<path:slug>/", settings_group, name="settings_group"),
    # The edited key rides in the PATH so a row carries no hidden inputs; the scope rides
    # in the query string beside it, since the global scope is the empty string.
    path("settings/set/<str:key>/", settings_set, name="settings_set"),
    path("settings/restore/<str:key>/", settings_restore, name="settings_restore"),
    path("settings/export/", settings_export, name="settings_export"),
    path("settings/import/", settings_import, name="settings_import"),
    path("tickets/<int:ticket_id>/", ticket_drawer, name="ticket_drawer"),
    path("tickets/<int:ticket_id>/transition/", ticket_transition, name="ticket_transition"),
    path("transcript/<str:session_id>/", transcript, name="transcript"),
    path("debug/session/", debug_session, name="debug_session"),
    path("debug/command/", command_run, name="command_run"),
]
