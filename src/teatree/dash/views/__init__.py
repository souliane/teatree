from teatree.dash.views.board import board, board_columns_partial
from teatree.dash.views.cycle_time import cycle_time
from teatree.dash.views.debug import command_run, debug_session
from teatree.dash.views.health import health, health_bands_partial
from teatree.dash.views.live import live, live_body_partial
from teatree.dash.views.loops import (
    gate_toggle,
    loop_action,
    loop_cadence,
    loops,
    loops_table_partial,
    posture,
    runner_toggle,
)
from teatree.dash.views.presets import (
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
)
from teatree.dash.views.sessions import sessions
from teatree.dash.views.settings import (
    settings,
    settings_export,
    settings_group,
    settings_import,
    settings_readouts,
    settings_restore,
    settings_set,
)
from teatree.dash.views.tickets import ticket_drawer, ticket_transition
from teatree.dash.views.transcript import transcript

__all__ = [
    "board",
    "board_columns_partial",
    "command_run",
    "cycle_time",
    "debug_session",
    "gate_toggle",
    "health",
    "health_bands_partial",
    "live",
    "live_body_partial",
    "loop_action",
    "loop_cadence",
    "loops",
    "loops_table_partial",
    "posture",
    "preset_create",
    "preset_delete",
    "preset_entry",
    "preset_meta",
    "preset_rename",
    "preset_use",
    "presets",
    "runner_toggle",
    "schedule_activate",
    "schedule_slot",
    "schedule_slot_delete",
    "sessions",
    "settings",
    "settings_export",
    "settings_group",
    "settings_import",
    "settings_readouts",
    "settings_restore",
    "settings_set",
    "ticket_drawer",
    "ticket_transition",
    "transcript",
]
