"""Single source of truth for the LIVE core migration set, derived from disk.

Two fleet-safety guards need the current set of ``core`` migrations and must
derive it identically or they silently drift. ``test_migration_squash_existing_db``
uses it to tell real ``django_migrations`` records (keep) from the fileless
pre-squash phantom rows (delete) — a stale set deletes the real records for any
migration it omits and then re-applies their ops onto already-present schema, a
``CreateModel`` "table already exists" brick. ``test_schema_guard`` uses it to
re-record the ledger rows it cleared while reproducing the #869 self-DB symptom.

The set MUST be derived from disk (never hardcoded) so it cannot fall out of sync
when a new migration lands — a hardcoded predecessor of exactly this set caused a
brick this cycle. Both consumers import this one derivation.
"""

from pathlib import Path

CORE_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree" / "core" / "migrations"


def core_migration_names() -> list[str]:
    """Every real on-disk ``core`` migration name (the numbered files), oldest first.

    Globs the numbered ``NNNN_*.py`` files (never ``__init__.py`` or
    ``max_migration.txt``) so the returned set is exactly the real migration graph.
    """
    return sorted(path.stem for path in CORE_MIGRATIONS_DIR.glob("[0-9]*.py"))
