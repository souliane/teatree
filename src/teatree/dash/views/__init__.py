from teatree.dash.views.board import board, board_columns_partial
from teatree.dash.views.config import config, config_bands_partial
from teatree.dash.views.debug import command_run, debug_session
from teatree.dash.views.health import health, health_bands_partial
from teatree.dash.views.loops import (
    availability,
    gate_toggle,
    loop_action,
    loop_cadence,
    loops,
    loops_table_partial,
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
from teatree.dash.views.tickets import ticket_drawer, ticket_transition

__all__ = [
    "availability",
    "board",
    "board_columns_partial",
    "command_run",
    "config",
    "config_bands_partial",
    "debug_session",
    "gate_toggle",
    "health",
    "health_bands_partial",
    "loop_action",
    "loop_cadence",
    "loops",
    "loops_table_partial",
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
    "ticket_drawer",
    "ticket_transition",
]
