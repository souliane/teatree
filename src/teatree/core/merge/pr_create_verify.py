"""Verify a just-created PR actually exists before trusting ``create_pr`` (#1194).

``create_pr`` returning a URL is not proof the PR is live: an eventual-consistency
race between the create and the next read, a mis-resolved cross-project mirror, or
a ``gh``/``glab`` exit-0 no-op can all hand back a URL for a PR a fresh GET does not
find. This is the verify-by-re-read seam for that gap — a single independent
re-read of the PR's open-state, distinct from the create call's own response,
factored out so both PR birthplaces (the ship/ensure create path and the manual-PR
reconciler) apply the same shape.

The re-read is CONFIRMED when the forge reports any concrete state (open / merged /
closed); it is NOT CONFIRMED only when the state comes back ``UNKNOWN`` — a 404, an
auth error, or an unparsable payload. ``get_pr_open_state`` already fails safe to
``UNKNOWN`` on any exception, so this helper never raises into its caller.
"""

from teatree.core.backend_protocols import CodeHostBackend, PrOpenState
from teatree.core.verify_by_reread import RereadOutcome, verify_by_reread

__all__ = ["verify_pr_exists"]


def verify_pr_exists(host: CodeHostBackend, url: str) -> RereadOutcome:
    """Re-read *url* on *host* and confirm the PR genuinely exists.

    Returns a :class:`RereadOutcome`: ``confirmed`` when the forge reports a
    concrete open/merged/closed state for the PR, ``not_confirmed`` when the
    re-read comes back ``UNKNOWN`` (404 / auth / unparsable). The caller gates
    on ``outcome.confirmed`` — a create whose re-read is not confirmed is
    reported failed, never recorded, so no phantom PR row/URL escapes.
    """
    return verify_by_reread(
        label=f"create_pr:{url}",
        reread=lambda: host.get_pr_open_state(pr_url=url) is not PrOpenState.UNKNOWN,
    )
