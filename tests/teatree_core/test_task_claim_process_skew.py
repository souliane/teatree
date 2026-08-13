"""The claim gate refuses the OTHER direction of skew — code behind schema (#4387).

``schema_behind_code()`` refuses when the DB lags the running code. Its mirror was
unguarded, and the mirror is the destructive one: a worker whose in-memory model
classes predate an applied migration keeps claiming, an agent runs to completion, and
the result is discarded at the record step because the INSERT omits a ``NOT NULL``
column. Three tasks were recorded as ``failed`` after ``exit 0`` that way.

The trap these pin is that a naive implementation is VACUOUS in exactly the dangerous
case. Django's ``MigrationLoader.load_disk()`` reloads the migrations package, so a
long-running process that re-reads disk sees the migration files the reinstall drain
fast-forwarded underneath it and reports CURRENT while its imported classes are old.
``TestTheVerdictIsFrozenAtStartup`` is the control for that: it moves the file on disk
AFTER the snapshot is frozen and asserts the refusal does not move.
"""

import os
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import django.test
from django.conf import settings
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import override_settings

from teatree.core.managers_task_claim import code_behind_schema, schema_behind_code
from teatree.core.models.task import Task
from teatree.core.process_freshness import (
    FreshnessVerdict,
    invalidate_process_freshness,
    read_process_freshness,
    record_loaded_snapshot,
    reset_loaded_snapshot,
)
from teatree.utils.throttled_log import reset_throttle
from tests.factories import TaskFactory

_FUTURE = "9999_a_column_this_process_never_loaded"


@contextmanager
def _sandbox_app_declaring_head(root: Path, head: str) -> Iterator[tuple[str, Path]]:
    """Install a throwaway app whose ``max_migration.txt`` this test owns and may rewrite.

    The live ``teatree.core`` file is shared mutable state other suites read
    concurrently (see ``test_linear_migrations``), so the disk-moves-underneath-you
    control gets its own app rather than touching it.
    """
    label = f"freshness_sandbox_{uuid.uuid4().hex}"
    migrations_dir = root / label / "migrations"
    migrations_dir.mkdir(parents=True)
    (root / label / "__init__.py").write_text("")
    (migrations_dir / "__init__.py").write_text("")
    head_file = migrations_dir / "max_migration.txt"
    head_file.write_text(f"{head}\n")

    sys.path.insert(0, str(root))
    try:
        with override_settings(INSTALLED_APPS=[*settings.INSTALLED_APPS, label]):
            yield label, head_file
    finally:
        if str(root) in sys.path:
            sys.path.remove(str(root))
        for module in [name for name in sys.modules if name == label or name.startswith(f"{label}.")]:
            del sys.modules[module]


class _ProcessSkewBase(django.test.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data_dir = tempfile.TemporaryDirectory(prefix="process-freshness-")
        self.addCleanup(self.data_dir.cleanup)
        env = patch.dict(os.environ, {"T3_DATA_DIR": self.data_dir.name, "TEATREE_ROLE": "worker"})
        env.start()
        self.addCleanup(env.stop)
        reset_throttle()
        invalidate_process_freshness()
        self.addCleanup(invalidate_process_freshness)

    def record_applied(self, app_label: str, name: str) -> None:
        MigrationRecorder(connection).migration_qs.create(app=app_label, name=name)

    def refreeze_real_snapshot(self) -> None:
        reset_loaded_snapshot()
        record_loaded_snapshot()


class TestCodeBehindSchemaDefersTheClaim(_ProcessSkewBase):
    def test_claim_next_pending_returns_none_and_leaves_the_task_pending(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        self.record_applied("core", _FUTURE)

        assert Task.objects.claim_next_pending(claimed_by="worker") is None
        assert Task.objects.filter(status=Task.Status.PENDING).count() == 1

    def test_claimable_is_empty_while_this_process_is_behind(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        self.record_applied("core", _FUTURE)

        assert Task.objects.claimable().count() == 0

    def test_the_existing_gate_is_blind_to_this_direction(self) -> None:
        """The asymmetry #4387 names, pinned: one gate sees it, the other cannot."""
        self.record_applied("core", _FUTURE)

        assert schema_behind_code() is False
        assert code_behind_schema() is True

    def test_the_refusal_names_both_heads_and_when_the_migration_was_applied(self) -> None:
        self.record_applied("core", _FUTURE)

        reading = read_process_freshness()

        assert reading.verdict is FreshnessVerdict.BEHIND
        assert reading.applied_head == _FUTURE
        assert reading.loaded_head
        assert reading.loaded_head != _FUTURE
        assert reading.applied_at
        assert "Restart this role's container when no task is claimed" in reading.block_reason()


class TestTheVerdictIsFrozenAtStartup(_ProcessSkewBase):
    def test_moving_the_head_file_on_disk_does_not_clear_the_refusal(self) -> None:
        """THE anti-vacuity control: a disk-re-reading predicate passes the tests above and fails here."""
        with (
            tempfile.TemporaryDirectory(prefix="freshness-app-") as root,
            _sandbox_app_declaring_head(Path(root), "0001_initial") as (label, head_file),
        ):
            self.addCleanup(self.refreeze_real_snapshot)
            reset_loaded_snapshot()
            record_loaded_snapshot()
            self.record_applied(label, "0002_the_migration_that_landed_after_boot")
            invalidate_process_freshness()

            assert read_process_freshness().verdict is FreshnessVerdict.BEHIND

            head_file.write_text("0002_the_migration_that_landed_after_boot\n")
            invalidate_process_freshness()

            assert read_process_freshness().verdict is FreshnessVerdict.BEHIND, (
                "the gate re-read disk: the reinstall drain fast-forwards the FILES while the "
                "imported classes stay old, so a disk read is the false green this exists to prevent"
            )


class TestTheGateStillAdmits(_ProcessSkewBase):
    def test_a_current_process_claims_normally(self) -> None:
        TaskFactory(status=Task.Status.PENDING)
        assert Task.objects.claimable().count() == 1

        claimed = Task.objects.claim_next_pending(claimed_by="worker")

        assert claimed is not None
        assert claimed.status == Task.Status.CLAIMED

    def test_an_applied_head_equal_to_the_loaded_one_is_not_behind(self) -> None:
        """A rolling deploy must not stall: applying the migration this code carries is CURRENT."""
        TaskFactory(status=Task.Status.PENDING)
        loaded = read_process_freshness()
        invalidate_process_freshness()
        self.record_applied("core", loaded.loaded_head)

        assert code_behind_schema() is False
        assert Task.objects.claim_next_pending(claimed_by="worker") is not None

    def test_an_absent_snapshot_admits_rather_than_locking_the_factory_out(self) -> None:
        """Fail OPEN on UNKNOWN: a no-snapshot refusal has no self-heal path, unlike the mirror gate."""
        TaskFactory(status=Task.Status.PENDING)
        self.addCleanup(self.refreeze_real_snapshot)
        reset_loaded_snapshot()
        invalidate_process_freshness()

        assert read_process_freshness().verdict is FreshnessVerdict.UNKNOWN
        assert code_behind_schema() is False
        assert Task.objects.claim_next_pending(claimed_by="worker") is not None
