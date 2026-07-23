"""URL config for the ``teatree.dash`` admin dashboard app (#3162).

Mounted at ``/dash/`` by the project URLconf. Full-page GETs render the three
pages (board / health / loops); the ``*_partial`` routes serve the htmx-poll
fragments; the POST routes are the CSRF-protected mutations.
"""

from django.urls import path
from django.views.generic.base import RedirectView

from teatree.dash.views import (
    availability,
    board,
    board_columns_partial,
    command_run,
    debug_session,
    gate_toggle,
    health,
    health_bands_partial,
    loop_action,
    loop_cadence,
    loops,
    loops_table_partial,
    preset_create,
    preset_delete,
    preset_entry,
    preset_meta,
    preset_rename,
    preset_use,
    presets,
    schedule_activate,
    schedule_slot,
    schedule_slot_delete,
    ticket_drawer,
    ticket_transition,
)

app_name = "dash"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="dash:board", permanent=False), name="index"),
    path("board/", board, name="board"),
    path("board/columns/", board_columns_partial, name="board_columns"),
    path("health/", health, name="health"),
    path("health/bands/", health_bands_partial, name="health_bands"),
    path("loops/", loops, name="loops"),
    path("loops/table/", loops_table_partial, name="loops_table"),
    path("loops/action/", loop_action, name="loop_action"),
    path("loops/availability/", availability, name="availability"),
    path("loops/gate/", gate_toggle, name="gate_toggle"),
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
    path("tickets/<int:ticket_id>/", ticket_drawer, name="ticket_drawer"),
    path("tickets/<int:ticket_id>/transition/", ticket_transition, name="ticket_transition"),
    path("debug/session/", debug_session, name="debug_session"),
    path("debug/command/", command_run, name="command_run"),
]
