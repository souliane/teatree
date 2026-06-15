"""The runtime self-DB schema pre-flight's own contract (souliane/teatree#2190).

Mirrors ``src/teatree/eval/regression_corpus_schema.py``. The corpus-level
orchestration (that ``run_regression_corpus`` calls the pre-flight before its
ORM checks) is covered in ``tests/agent_behavior/replay/test_regression_corpus.py``; this file
exercises the pre-flight module in isolation: a clean migrate yields a GREEN
result, and a failing migrate fails LOUD (fail-closed) rather than passing
silently against a half-migrated DB.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates.schema_guard import SelfDbMigrationError
from teatree.eval import regression_corpus_schema
from teatree.eval.regression_corpus_schema import SCHEMA_PREFLIGHT, migrate_self_db, schema_preflight_result


class TestSchemaPreflightCheckDefinition(TestCase):
    def test_check_is_db_backed_and_names_2190(self) -> None:
        assert SCHEMA_PREFLIGHT.needs_db is True
        assert "#2190" in SCHEMA_PREFLIGHT.failure_class


class TestSchemaPreflightResult(TestCase):
    def test_clean_migrate_yields_green_result(self) -> None:
        with patch.object(regression_corpus_schema, "migrate_self_db", return_value=[]) as migrate:
            result = schema_preflight_result()
        migrate.assert_called_once()
        assert result.ok is True
        assert result.skipped is False
        assert result.detail == ""
        assert result.check is SCHEMA_PREFLIGHT

    def test_pending_labels_applied_still_green(self) -> None:
        applied = ["core.0062_worktree_last_used_at"]
        with patch.object(regression_corpus_schema, "migrate_self_db", return_value=applied):
            result = schema_preflight_result()
        assert result.ok is True

    def test_failing_migrate_fails_closed_carrying_the_error(self) -> None:
        with patch.object(
            regression_corpus_schema,
            "migrate_self_db",
            side_effect=SelfDbMigrationError("migrate blew up on last_used_at"),
        ):
            result = schema_preflight_result()
        assert result.ok is False, "a failed runtime-schema migrate must fail-closed, never pass silently"
        assert result.skipped is False
        assert "migrate blew up on last_used_at" in result.detail


class TestMigrateSelfDbSeam(TestCase):
    def test_binding_delegates_to_schema_guard(self) -> None:
        with patch("teatree.core.gates.schema_guard.migrate_self_db", return_value=["core.0063"]) as guard:
            applied = migrate_self_db()
        guard.assert_called_once_with()
        assert applied == ["core.0063"]

    def test_binding_propagates_migration_error(self) -> None:
        with (
            patch("teatree.core.gates.schema_guard.migrate_self_db", side_effect=SelfDbMigrationError("boom")),
            pytest.raises(SelfDbMigrationError),
        ):
            migrate_self_db()
