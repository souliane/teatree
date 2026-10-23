"""Quick-check findings retain every row across both SQLite callers."""

from unittest.mock import MagicMock

from django.db import DatabaseError

from teatree.sqlite_integrity import quick_check_problems


def test_reports_every_problem_as_text() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchall.return_value = [("bad page",), (17,)]

    assert quick_check_problems(connection) == ["bad page", "17"]
    cursor.execute.assert_called_once_with("PRAGMA quick_check")


def test_django_cursor_error_is_a_finding() -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.execute.side_effect = DatabaseError("unable to read")

    assert quick_check_problems(connection) == ["unable to read"]


def test_django_cursor_open_error_is_a_finding() -> None:
    connection = MagicMock()
    connection.cursor.side_effect = DatabaseError("unable to open")

    assert quick_check_problems(connection) == ["unable to open"]
