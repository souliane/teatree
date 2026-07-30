"""Reference-DB migrate vocabulary.

The tri-state outcome the importer's migrate loop returns, plus the retry
cap. The migrate loop itself lives on
:class:`~teatree.utils.django_db.importer.DjangoDbImporter` — it is
inseparable from the importer's per-run state and CLI streams; this module
owns only the outcome vocabulary the restore strategies branch on.
"""

import enum

_MAX_MIGRATE_RETRIES = 20


class _MigrateResult(enum.Enum):
    APPLIED = "applied"
    ALREADY_MIGRATED = "already_migrated"
    FAILED = "failed"
