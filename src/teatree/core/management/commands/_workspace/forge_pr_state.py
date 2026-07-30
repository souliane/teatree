"""Concrete forge reader for the open-PR teardown gate.

The gate itself (:mod:`teatree.core.gates.open_pr_teardown_gate`) is policy and
stays free of concrete backends — ``teatree.core`` may not import
``teatree.backends``, which depends on it. This module is the interface-layer
half that resolves the code host and reads one PR's live state, mirroring the
``pr_budget_gate`` (policy) / ``pr_budget_forge`` (forge access) split.

Exceptions are left to propagate: the gate owns the fail-closed policy and maps
anything it cannot read to ``UNKNOWN``.
"""

from teatree.backends.loader import get_code_host_for_url
from teatree.core.backend_protocols import PrOpenState
from teatree.core.overlay_loader import get_overlay_for_url


def read_live_pr_state(pr_url: str) -> PrOpenState:
    """The forge's own state for *pr_url*, ``UNKNOWN`` when no host resolves."""
    host = get_code_host_for_url(get_overlay_for_url(pr_url), pr_url)
    if host is None:
        return PrOpenState.UNKNOWN
    return host.get_pr_open_state(pr_url=pr_url)
