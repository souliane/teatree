"""Transition runners — composed work executed by ``@task`` workers.

Each runner performs the long I/O for a specific lifecycle transition.
Workers enqueue at transition time, claim via ``select_for_update()``, and
on success call the next transition to advance the ticket.

See BLUEPRINT.md §4 for the invariant and §4.1 for the per-transition map.
"""

from teatree.core.runners.base import RunnerBase
from teatree.core.runners.provision import WorktreeProvisioner
from teatree.core.runners.retro import RetroExecutor
from teatree.core.runners.ship import ShipExecutor
from teatree.core.runners.teardown import WorktreeTeardown

__all__ = [
    "RetroExecutor",
    "RunnerBase",
    "ShipExecutor",
    "WorktreeProvisioner",
    "WorktreeTeardown",
]
