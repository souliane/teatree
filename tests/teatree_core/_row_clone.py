"""Copy a row at the SQL level with one column left out, the INSERT an older model class emits."""

from django.db import connection


def clone_row_omitting(table: str, column: str, pk: int, *, replacing: dict[str, str] | None = None) -> None:
    """Re-insert row *pk* of *table* with every column except ``id`` and *column*.

    The generic shape matters: it is what a model class that predates *column*
    emits, and it stays correct as the table grows more columns. *replacing*
    substitutes a bound value for a column the copy cannot duplicate (a unique
    ``name``), so the only thing under test stays the omission.
    """
    substitutions = replacing or {}
    with connection.cursor() as cursor:
        names = [field.name for field in connection.introspection.get_table_description(cursor, table)]
        copied = [name for name in names if name not in {"id", column}]
        targets = ", ".join(connection.ops.quote_name(name) for name in copied)
        sources = ", ".join("%s" if name in substitutions else connection.ops.quote_name(name) for name in copied)
        quoted_table = connection.ops.quote_name(table)
        cursor.execute(
            " ".join(
                [
                    "INSERT INTO",
                    quoted_table,
                    "(" + targets + ")",
                    "SELECT",
                    sources,
                    "FROM",
                    quoted_table,
                    "WHERE id = %s",
                ]
            ),
            [*(substitutions[name] for name in copied if name in substitutions), pk],
        )
