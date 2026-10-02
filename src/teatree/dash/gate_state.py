"""Shared read of the master ``danger_gate_fail_open`` switch for the dashboard.

Both the health-bands mode band and the loop-control header render whether the
master fail-open switch is on (a red banner when it is). Sharing one guarded
helper keeps the two pages from drifting and ensures neither 500s on a broken
read — the loop-control page previously read it unguarded while its health twin
wrapped the read in try/except.
"""

import logging

from teatree.core.models.config_setting import ConfigSetting

logger = logging.getLogger(__name__)

_GATE_KEY = "danger_gate_fail_open"


def dash_gate_fail_open() -> bool:
    """Whether the master ``danger_gate_fail_open`` switch is on (a red banner when it is).

    Reads the DB-home value through the ORM — the same store the dashboard's own
    gate toggle writes to. Fails CLOSED to ``False`` on a broken read so the
    banner never falsely alarms and the page never 500s.
    """
    try:
        stored = ConfigSetting.objects.get_effective(_GATE_KEY)
    except Exception:
        logger.warning("dash %s read failed — treating as closed", _GATE_KEY, exc_info=True)
        return False
    return bool(stored)
