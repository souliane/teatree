"""Send-proxy routing for colleague-visible forge comments (#117).

Extracted from :mod:`teatree.cli.review.service` (mirroring the
:mod:`teatree.cli.review.post_impl` extraction) so that module stays under the
module-health LOC ceiling.
"""

from teatree.core.send_proxy import SendChannel, SendRequest, route_send


def route_forge_send(*, repo: str, mr: int, action: str, note: str) -> tuple[str, str]:
    """Route a colleague-visible forge comment through the #117 send-proxy.

    Returns ``(routed_note, refusal)``: ``refusal`` is ``""`` when the proxy
    allows the send (the ship-default ``warn`` mode always allows and returns
    ``note`` unchanged — audit-only), and a human-readable message when the
    proxy refuses the destination in ``enforce`` mode. ``routed_note`` is the
    body to post (redacted in ``enforce`` mode). One
    :class:`~teatree.core.models.send_audit.SendAudit` row is written per
    live comment, feeding the destination soak the operator seeds the
    allowlist from.
    """
    verdict = route_send(
        SendRequest(
            channel=SendChannel.GITLAB,
            destination=repo,
            payload=note,
            action=action,
            target=f"{repo}!{mr}",
        ),
    )
    if not verdict.allowed:
        return note, verdict.reason or f"send-proxy refused posting to {repo}"
    return verdict.payload, ""
