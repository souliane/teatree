"""SQLite backend that can only open a container-owned database read-only.

Django's stock SQLite backend is subclassed at its one connection-construction
seam — :meth:`DatabaseWrapper.get_connection_params` — so the invariant in
:mod:`teatree.db.boundary` is *structural*: on a host, a claimed database is
handed to ``sqlite3.connect`` as a ``mode=ro`` URI with ``PRAGMA query_only=1``
set on the resulting connection. There is no code path left that reopens it
read-write by accident, because every Django connection in the project is built
here.

The read-only downgrade is silent by design. Every host consumer that survives
today — the Claude Code hooks' ``django.setup()`` reads, the statusline, ad-hoc
inspection — only reads, and failing those loudly at open time would take out the
session lifecycle to prevent a write they never attempt. A *write* is what must
fail loudly, so :class:`_ReadOnlyDomainCursor` turns SQLite's terse "attempt to
write a readonly database" into the boundary's remedy.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from sqlite3 import Connection, Cursor, OperationalError
from typing import Any, cast

from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper
from django.db.backends.sqlite3.base import SQLiteCursorWrapper

from teatree.db.boundary import ControlDbBoundary, DbBoundaryError

type SqliteParam = str | int | float | bytes | None
type SqliteParams = Sequence[SqliteParam] | Mapping[str, SqliteParam]

_READ_ONLY_MARKERS = ("readonly database", "read-only database", "query_only")


def _is_read_only_refusal(error: Exception) -> bool:
    return any(marker in str(error) for marker in _READ_ONLY_MARKERS)


class _ReadOnlyDomainCursor:
    """Wraps Django's SQLite cursor, re-raising a refused write as :class:`DbBoundaryError`.

    Composition rather than a :class:`SQLiteCursorWrapper` subclass. The only
    thing this adds is translating one exception, and subclassing would bind it
    to ``sqlite3.Cursor``'s stricter contract (positional-only, no ``None``
    parameters) that Django's own wrapper does not itself honour. Django reaches
    what :meth:`DatabaseWrapper.create_cursor` returns only through
    ``BaseDatabaseWrapper._prepare_cursor`` → ``CursorWrapper``, which is duck
    typed throughout — no ``isinstance`` and no reach for the raw DB-API cursor
    on this backend — so a delegate is indistinguishable at every call site.

    Only installed on downgraded connections; an owning-domain connection keeps
    Django's cursor untouched.
    """

    def __init__(self, cursor: SQLiteCursorWrapper, boundary: ControlDbBoundary) -> None:
        self._cursor = cursor
        self._boundary = boundary

    def execute(self, sql: str, parameters: SqliteParams | None = None) -> Cursor:
        try:
            if parameters is None:
                return self._cursor.execute(sql)
            return self._cursor.execute(sql, parameters)
        except OperationalError as error:
            raise self._translate(error) from error

    def executemany(self, sql: str, seq_of_parameters: Iterable[SqliteParams]) -> Cursor:
        try:
            return self._cursor.executemany(sql, seq_of_parameters)
        except OperationalError as error:
            raise self._translate(error) from error

    def _translate(self, error: OperationalError) -> Exception:
        return DbBoundaryError(self._boundary.refusal()) if _is_read_only_refusal(error) else error

    def __getattr__(self, name: str) -> object:
        return getattr(self._cursor, name)

    def __iter__(self) -> Iterator[object]:
        return iter(self._cursor)


type BoundaryCursor = Cursor | _ReadOnlyDomainCursor


class DatabaseWrapper(SQLiteDatabaseWrapper):
    """SQLite wrapper that downgrades a container-owned database to read-only on a host."""

    boundary_read_only = False

    # ``django-types`` stubs both connection-construction hooks ``-> None``; Django
    # returns the ``sqlite3.connect`` kwargs here and a cursor from ``create_cursor``
    # (``django-stubs`` types both correctly, so the stub is the defect). Return types
    # are covariant, so no truthful override satisfies it: the casts carry the bodies,
    # the ignores the signatures, and ``unused-ignore-comment = error`` expires both.
    def get_connection_params(self) -> dict[str, Any]:  # ty: ignore[invalid-method-override]
        params = cast("dict[str, Any]", super().get_connection_params())
        boundary = ControlDbBoundary(self.settings_dict["NAME"])
        self.boundary_read_only = False
        if not boundary.file_backed:
            return params
        if boundary.read_write_allowed:
            boundary.claim_for_container()
            return params

        self.boundary_read_only = True
        self._boundary = boundary
        params["database"] = boundary.readonly_uri
        # The production OPTIONS' ``PRAGMA journal_mode=…`` needs the write lock;
        # ``query_only`` is what a read-only connection wants in its place.
        self.init_commands = ["PRAGMA query_only=1"]
        # ``BEGIN IMMEDIATE`` takes SQLite's reserved write lock at transaction
        # start, so it would fail a read-only ``atomic()`` block that never writes.
        # Deferred ``BEGIN`` lets those run and defers the refusal to a real write.
        self.transaction_mode = None
        return params

    def create_cursor(self, name: str | None = None) -> BoundaryCursor:  # ty: ignore[invalid-method-override]
        if not self.boundary_read_only:
            return cast("Cursor", super().create_cursor(name))
        connection: Connection = self.connection
        return _ReadOnlyDomainCursor(connection.cursor(factory=SQLiteCursorWrapper), self._boundary)
