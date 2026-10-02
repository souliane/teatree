"""The comparability stamp — whether two snapshots may be diffed at all, and what schema each carries.

The settings half hard-FAILS: a settings diff taken across two boxes that disagree about
what settings exist is worse than no diff at all.

The schema half is fail-SOFT, and a field it could not read is left OUT rather than filled
in. That is deliberate: an empty digest emitted on failure would be EQUAL on two boxes that
both failed, and would read as agreement.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Final

from django.db import connections
from django.db.migrations.loader import MigrationLoader

from teatree.config.cold_defaults import DEFAULTS_TOML
from teatree.config.seed_defaults import SEED_ROW_FIELDS, SHIPPED_ONLY_FIELDS
from teatree.core.schema_readiness import pending_migrations
from teatree.core.settings_snapshot.serialisation import Json, digest, serialise

logger = logging.getLogger(__name__)

# The physical schema, read from the database rather than from the migration history. Both are
# constants: nothing here is ever assembled from a captured string.
_TABLE_NAMES_SQL: Final = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
_TABLE_COLUMNS_SQL: Final = 'SELECT name, type, "notnull", pk FROM pragma_table_info(%s) ORDER BY name'

#: One setting key's declared type as stable text, plus the values it admits.
type Shape = tuple[str, tuple[object, ...]]


class SnapshotError(RuntimeError):
    """A required source failed, so no honest snapshot can be produced."""


@dataclass
class Warnings:
    """The degraded-source log: an optional source that raises is recorded, never fatal.

    The exception's TEXT never rides along, only its type. A coercion error quotes the value
    it refused, and this log is served to another instance — so the payload that promises to
    carry no raw secret would have carried one through its own diagnostics. The full traceback
    is logged on the box, where it belongs.
    """

    messages: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.messages.append(message)

    def optional[T](self, source: str, call: Callable[[], T], fallback: T) -> T:
        try:
            return call()
        except Exception as exc:
            logger.exception("optional snapshot source %r failed", source)
            self.add(f"{source}: {type(exc).__name__} — see this instance's log for the detail")
            return fallback


def build_fingerprint(shapes: Mapping[str, Shape], warn: Warnings) -> dict[str, Any]:
    """The whole stamp: the hard-failing settings half, then the fail-soft schema half."""
    try:
        triples = [[key, text, [serialise(choice) for choice in choices]] for key, (text, choices) in shapes.items()]
        settings_stamp = {
            "settings_schema_sha256": digest(triples),
            "settings_key_count": len(shapes),
            "defaults_toml_sha256": digest(DEFAULTS_TOML.read_text(encoding="utf-8")),
            "seed_fields_sha256": digest(_seed_field_shape()),
        }
    except Exception as exc:
        logger.exception("the fingerprint could not be built")
        message = f"the fingerprint could not be built: {type(exc).__name__} — see this instance's log"
        raise SnapshotError(message) from exc
    return {**settings_stamp, **schema_state(warn)}


def schema_state(warn: Warnings) -> dict[str, Any]:
    """What schema this box actually carries — read from the database, not from migration history.

    A digest over applied migration ROWS is not a schema fingerprint. A squashed migration keeps
    rows for migrations that no longer exist on disk, and an app removed from the code keeps its
    rows too, so a long-lived box and a freshly provisioned one disagree while carrying an
    identical schema. The honest answer is the columns themselves.
    """
    state: dict[str, Any] = {}
    columns = warn.optional("physical schema via pragma_table_info", _table_columns, None)
    if columns is not None:
        state["physical_schema_sha256"] = digest(columns)
        state["table_shapes"] = {table: digest(cols) for table, cols in columns.items()}
        state["table_count"] = len(columns)
    state.update(warn.optional("django.db.migrations.loader.MigrationLoader", _migration_graph, {}))
    pending = warn.optional("core.schema_readiness.pending_migrations", _pending, None)
    if pending is not None:
        state["pending_migrations"] = pending
    if release := warn.optional("importlib.metadata.version('django')", _django_version, ""):
        state["django_version"] = release
    return state


def _seed_field_shape() -> dict[str, Any]:
    return {
        "SEED_ROW_FIELDS": {
            table: {name: [attr, python_type.__name__] for name, (attr, python_type) in fields.items()}
            for table, fields in SEED_ROW_FIELDS.items()
        },
        "SHIPPED_ONLY_FIELDS": {table: list(names) for table, names in SHIPPED_ONLY_FIELDS.items()},
    }


def _table_columns() -> dict[str, Json]:
    """Every table's column list, normalised: name, declared type, not-null and primary-key flag."""
    with connections["default"].cursor() as cursor:
        cursor.execute(_TABLE_NAMES_SQL)
        tables = [str(row[0]) for row in cursor.fetchall()]
        shape: dict[str, Json] = {}
        for table in tables:
            cursor.execute(_TABLE_COLUMNS_SQL, [table])
            shape[table] = [[serialise(part) for part in row] for row in cursor.fetchall()]
    return shape


def _migration_graph() -> dict[str, Any]:
    """The leaf per app — plus the applied-row count, which is context and never a verdict."""
    loader = MigrationLoader(connections["default"])
    return {
        "migration_leaves": {str(app): str(name) for app, name in sorted(loader.graph.leaf_nodes())},
        "applied_migration_count": len(loader.applied_migrations),
    }


def _pending() -> list[str]:
    """Empty when the box is up to date; anything else means mid-deploy — its readings are provisional.

    teatree's own readiness check rather than a node-minus-applied subtraction, because that
    subtraction is the very mistake this fingerprint exists to undo: it miscounts a squash.
    """
    return [str(node) for node in pending_migrations()]


def _django_version() -> str:
    """Reported beside the schema digest, never folded into it.

    A column's declared type is recorded DDL text, stable per Django version, so an upgrade
    moves the digest. Kept outside it, that reads as an explainable context difference; folded
    in, it would read as an unexplained hash mismatch.
    """
    try:
        return version("django")
    except PackageNotFoundError:
        return ""


__all__ = ["Shape", "SnapshotError", "Warnings", "build_fingerprint", "schema_state"]
