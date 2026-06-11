"""Persist conversation-audit outcomes into the durable ledger.

Sibling of :mod:`teatree.eval.persistence`: that module persists a metered eval
run, this one persists the audit pass over captured sessions. One transaction
per call so a partially-written audit batch never pollutes the ledger.
"""

from collections.abc import Sequence

from django.db import transaction

from teatree.core.models import SessionAuditRecord
from teatree.eval.persistence import current_git_sha


def persist_audit(
    records: Sequence[SessionAuditRecord],
    *,
    git_sha: str | None = None,
) -> list[SessionAuditRecord]:
    """Write each audit record in one transaction, stamping the run's ``git_sha``.

    Each element is an unsaved :class:`SessionAuditRecord`; the provenance
    ``git_sha`` is filled from the current checkout when not supplied.
    """
    sha = current_git_sha() if git_sha is None else git_sha
    with transaction.atomic():
        for record in records:
            record.git_sha = record.git_sha or sha
            record.save()
    return list(records)
