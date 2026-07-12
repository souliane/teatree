"""Outbound-claim audit recording for GitHub comment publishes (#1198/#1275).

Split from :mod:`teatree.backends.github` so the code-host module stays
under the module-health LOC cap. :func:`record_github_note_claim` is the
GitHub-side parallel of :func:`teatree.cli.review.audit.record_note_claim`
for the GitLab side: a best-effort write of one :class:`OutboundClaim`
row in the same logical step as a successful comment POST, so the drift
verifier can later confirm the artifact actually exists on the forge.

Every dependency is imported lazily inside the function body so importing
the code-host module never eagerly boots Django.
"""


def record_github_note_claim(
    *,
    repo: str,
    target_number: int,
    comment_id: int,
    body: str,
    target_url: str,
) -> None:
    """Audit one successful GitHub-comment publish for the drift verifier (#1198).

    Mirrors :func:`teatree.cli.review.audit.record_note_claim` for the
    GitLab side: best-effort write, never raises into the caller.
    ``payload_digest`` lets the verifier detect silent body-divergence
    without storing the full body in the claim row.

    The idempotency key encodes ``repo``, target number (PR or issue —
    GitHub uses the same ``/issues/<n>/comments`` endpoint for both), and
    the server-assigned ``comment_id`` so a retried POST that the API
    collapsed to the same comment no-ops at the ledger layer.

    Best-effort: any exception (Django not booted, DB outage, integrity
    race) is swallowed. The publish has already succeeded by the time we
    get here — failing to audit it must not turn that success into a
    user-visible failure.

    Stamps ``extra["overlay"]`` from ``T3_OVERLAY_NAME`` (#1275) so the
    audit verifier can re-read the comment through the same overlay's
    GitHub token that posted it — not a process-global resolver that may
    land on a different identity in multi-overlay setups.
    """
    import hashlib  # noqa: PLC0415 — stdlib, cheap, used only here
    import os  # noqa: PLC0415 — defer stdlib import out of module load

    try:
        from django.db import (  # noqa: PLC0415 — keep Django out of module-load if bootstrap fails
            DatabaseError,
            IntegrityError,
            transaction,
        )

        from teatree.core.models import OutboundClaim  # noqa: PLC0415 — deferred: ORM import needs the app registry
    except Exception:  # noqa: BLE001 — must never break the publish path
        return

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    overlay_name = os.environ.get("T3_OVERLAY_NAME", "") or ""
    idempotency_key = f"github_note:{repo}#{target_number}:{comment_id}"
    try:
        with transaction.atomic():
            OutboundClaim.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    "kind": OutboundClaim.Kind.GITHUB_NOTE.value,
                    "target_url": target_url,
                    "extra": {
                        "repo": repo,
                        "target_number": target_number,
                        "artifact_id": str(comment_id),
                        "payload_digest": digest,
                        "overlay": overlay_name,
                    },
                },
            )
    except (IntegrityError, DatabaseError):
        return
    except Exception:  # noqa: BLE001 — must never break the publish path
        return
