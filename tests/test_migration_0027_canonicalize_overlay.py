"""Regression tests for ``core.0027_canonicalize_teatree_overlay``.

souliane/teatree#1154: migration 0027 originally issued a bulk
``filter(overlay='teatree').update(overlay='t3-teatree')``. On any model
whose unique constraint involves ``overlay`` plus another field, a legacy
``teatree`` row sharing the other-field values with an existing
``t3-teatree`` row raises ``IntegrityError`` (UNIQUE constraint failed),
which then poisons the surrounding transaction.

These tests pin the merge behaviour: a legacy row that collides with a
canonical twin is deleted; non-colliding legacy rows are still
canonicalized; the multi-field constraint path (``ReviewAssignment``) is
exercised so the constraint iteration is not silently single-field.
"""

import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

# The migration module starts with a digit so it cannot be imported via the
# normal ``from ... import`` syntax; use ``importlib`` directly.
_migration_module = importlib.import_module("teatree.core.migrations.0027_canonicalize_teatree_overlay")


class CanonicalizeTeatreeOverlayCollisionMigrationTest(TransactionTestCase):
    """0027 must merge legacy ``teatree`` rows that collide with canonical twins."""

    _BEFORE = ("core", "0026_pending_chat_loop_reply_fields")
    _AFTER = ("core", "0027_canonicalize_teatree_overlay")

    def _migrate(self, target: tuple[str, str]) -> "object":
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor.loader.build_graph()
        return executor.loader.project_state([target]).apps

    def _restore_head(self) -> None:
        """Re-apply 0027 so TransactionTestCase teardown flushes the real schema."""
        self._migrate(self._AFTER)

    def test_pending_chat_legacy_row_with_canonical_twin_is_deleted(self) -> None:
        apps = self._migrate(self._BEFORE)
        pending = apps.get_model("core", "PendingChatInjection")
        now = timezone.now()
        canonical = pending.objects.create(
            overlay="t3-teatree", channel="C1", slack_ts="1700000000.000100", text="canonical", received_at=now
        )
        legacy = pending.objects.create(
            overlay="teatree", channel="C1", slack_ts="1700000000.000100", text="legacy", received_at=now
        )

        _migration_module._canonicalize_teatree_overlay(apps, connection.schema_editor())

        survivors = list(pending.objects.filter(slack_ts="1700000000.000100"))
        assert len(survivors) == 1, f"expected one survivor, got {[(s.pk, s.overlay, s.text) for s in survivors]}"
        assert survivors[0].pk == canonical.pk, "canonical row must be the survivor"
        assert survivors[0].overlay == "t3-teatree"
        assert not pending.objects.filter(pk=legacy.pk).exists(), "legacy colliding row must be deleted"

        self._restore_head()

    def test_pending_chat_legacy_row_without_twin_is_canonicalized(self) -> None:
        apps = self._migrate(self._BEFORE)
        pending = apps.get_model("core", "PendingChatInjection")
        now = timezone.now()
        legacy = pending.objects.create(
            overlay="teatree", channel="C1", slack_ts="1700000000.000200", text="legacy-only", received_at=now
        )

        _migration_module._canonicalize_teatree_overlay(apps, connection.schema_editor())

        legacy.refresh_from_db()
        assert legacy.overlay == "t3-teatree"
        assert pending.objects.filter(slack_ts="1700000000.000200").count() == 1

        self._restore_head()

    def test_review_assignment_multi_field_constraint_collision_merges(self) -> None:
        """Exercise the multi-field (overlay, mr_url, user_id) constraint path."""
        apps = self._migrate(self._BEFORE)
        review = apps.get_model("core", "ReviewAssignment")
        now = timezone.now()
        canonical = review.objects.create(
            overlay="t3-teatree",
            mr_url="https://example.com/mr/9001",
            user_id="U9001",
            channel="C9001",
            slack_ts="1700000000.900100",
            observed_at=now,
        )
        legacy = review.objects.create(
            overlay="teatree",
            mr_url="https://example.com/mr/9001",
            user_id="U9001",
            channel="C9001",
            slack_ts="1700000000.900101",
            observed_at=now,
        )

        _migration_module._canonicalize_teatree_overlay(apps, connection.schema_editor())

        survivors = list(review.objects.filter(mr_url="https://example.com/mr/9001", user_id="U9001"))
        assert len(survivors) == 1
        assert survivors[0].pk == canonical.pk
        assert survivors[0].overlay == "t3-teatree"
        assert not review.objects.filter(pk=legacy.pk).exists()

        self._restore_head()

    def test_review_assignment_non_colliding_legacy_row_is_canonicalized(self) -> None:
        apps = self._migrate(self._BEFORE)
        review = apps.get_model("core", "ReviewAssignment")
        now = timezone.now()
        legacy = review.objects.create(
            overlay="teatree",
            mr_url="https://example.com/mr/9002",
            user_id="U9002",
            channel="C9002",
            slack_ts="1700000000.900200",
            observed_at=now,
        )

        _migration_module._canonicalize_teatree_overlay(apps, connection.schema_editor())

        legacy.refresh_from_db()
        assert legacy.overlay == "t3-teatree"

        self._restore_head()

    def test_pending_chat_many_collisions_are_all_merged(self) -> None:
        """Mirror the real-DB shape: many legacy rows colliding on shared slack_ts."""
        apps = self._migrate(self._BEFORE)
        pending = apps.get_model("core", "PendingChatInjection")
        now = timezone.now()
        canonicals: list[object] = []
        for i in range(5):
            ts = f"1700000000.0003{i:02d}"
            canonicals.append(
                pending.objects.create(
                    overlay="t3-teatree", channel="C1", slack_ts=ts, text=f"canonical-{i}", received_at=now
                )
            )
            pending.objects.create(overlay="teatree", channel="C1", slack_ts=ts, text=f"legacy-{i}", received_at=now)

        _migration_module._canonicalize_teatree_overlay(apps, connection.schema_editor())

        for c in canonicals:
            survivors = pending.objects.filter(slack_ts=c.slack_ts)
            assert survivors.count() == 1
            assert survivors.first().pk == c.pk
        assert not pending.objects.filter(overlay="teatree").exists()

        self._restore_head()
