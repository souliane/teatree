"""Send-proxy routing for colleague-visible forge comments (#117).

Extracted from :mod:`teatree.cli.review.service` (mirroring the
:mod:`teatree.cli.review.post_impl` extraction) so that module stays under the
module-health LOC ceiling.
"""

from teatree.core.send_proxy import OutboundBlockedError, SendChannel, route_forge_write


def route_forge_send(*, repo: str, mr: int, action: str, note: str) -> tuple[str, str]:
    """Route a colleague-visible MR comment through the SCANNED forge-write seam.

    Delegates to :func:`~teatree.core.send_proxy.route_forge_write` — the ONE
    content-bearing forge-write chokepoint — so an MR comment runs the SAME
    public-repo leak gate + #117 send-proxy (per-overlay allowlist + redaction +
    one :class:`~teatree.core.models.send_audit.SendAudit` row) that every other
    forge writer runs. This collapses the earlier divergence where this router
    audited the send but did NOT leak-scan it, so a caller could reach a
    colleague surface on a laxer path than the MCP / CLI forge writers.

    Returns ``(routed_note, refusal)``: ``refusal`` is ``""`` when the seam
    allows the send (the ``warn`` ship default always allows and returns ``note``
    unchanged — audit-only) and a human-readable message when the seam refuses —
    a public-repo leak (:class:`~teatree.core.send_proxy.OutboundLeakError`) or a
    non-allowlisted destination in ``enforce`` mode
    (:class:`~teatree.core.send_proxy.SendBlockedError`). ``routed_note`` is the
    body to post (redacted in ``enforce`` mode). An empty note is a no-op
    pass-through (no scan, no audit). ``t3 review`` is a GitLab-only surface, so
    the forge is pinned to GitLab for the visibility probe and the audit channel.
    """
    try:
        routed = route_forge_write(
            forge=SendChannel.GITLAB.value,
            repo=repo,
            text=note,
            action=action,
            target=f"{repo}!{mr}",
        )
    except OutboundBlockedError as blocked:
        return note, str(blocked)
    return routed, ""
