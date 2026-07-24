# test-path: cross-cutting
"""Acceptance gate for the ``0001_squashed_0030`` core squash (souliane/teatree#3676).

The squash collapses the ``0001..0030`` chain into one migration that carries
``replaces``, added ALONGSIDE the still-present replaced files so a live deployed DB
keeps its upgrade path. Django preserves ``RunPython`` in a squash but asks the
author to hand-copy the bodies; the risk is a seed/backfill data op silently dropped
or reordered. This gate proves the squash is behaviour-preserving on the two cases
that matter:

(a) FRESH-INSTALL EQUIVALENCE — a DB migrated through the UNSQUASHED chain ends with
    the same seeded rows (Loop, Prompt, Mode, ConfigSetting) as a DB migrated through
    the SQUASHED path. The unsquashed chain is forced with a
    ``MigrationLoader(replace_migrations=False)`` executor so the individual
    ``0001..0030`` migrations apply instead of the squash.

(b) DEPLOYED-CASE NO-OP — a DB already at ``0030`` via the individual chain, with the
    squash present but not yet recorded, applies ``migrate`` as a pure no-op via
    ``replaces``: Django records the squash applied WITHOUT running its operations
    (all replaced migrations are already applied), so the seeded data is untouched.

Anti-vacuity: the equivalence assertion compares two NON-EMPTY snapshots and the test
asserts the seeds actually produced rows (``arch_review`` prompt, ``offline`` mode,
loops present). Drop any seed ``RunPython`` from the squash and the squashed snapshot
loses those rows while the unsquashed one keeps them → RED (verified by temporarily
removing ``_seed_default_loops`` from the squash during development).
"""

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase

_SQUASH = "0001_squashed_0030"
_LAST_REPLACED = "0030_review_verdict_reviewer_identity_normalized"

# Seeded model → natural key. These are every model a replaced migration seeds or
# transforms on a fresh install: Loop + Prompt (0001 ``_seed_default_loops``), Mode
# (0022 ``_seed_offline_mode``, the LoopPreset renamed at 0023), and ConfigSetting
# (0027 ``_carry_configured_values`` — empty on a fresh DB, included for completeness).
_SEEDED_KEYS = {
    "Loop": lambda o: o.name,
    "Prompt": lambda o: o.name,
    "Mode": lambda o: o.name,
    "ConfigSetting": lambda o: (o.scope, o.key),
}
# Excluded from the byte comparison: the auto pk and the two wall-clock timestamps,
# which legitimately differ between two migration runs. Everything else is seed DATA.
_VOLATILE_FIELDS = frozenset({"id", "created_at", "updated_at"})


def _snapshot() -> dict[str, dict]:
    """Every seeded model's rows as {natural_key: {field: value}}, FK rendered by name."""
    out: dict[str, dict] = {}
    for model_name, key_of in _SEEDED_KEYS.items():
        model = apps.get_model("core", model_name)
        rows: dict = {}
        for obj in model.objects.all():
            record = {}
            for field in model._meta.concrete_fields:
                if field.name in _VOLATILE_FIELDS:
                    continue
                if field.is_relation:
                    related = getattr(obj, field.name)
                    record[field.name] = getattr(related, "name", None) if related is not None else None
                else:
                    record[field.name] = getattr(obj, field.name)
            rows[key_of(obj)] = record
        out[model_name] = rows
    return out


@pytest.mark.timeout(240)
class TestCoreMigrationSquash(TransactionTestCase):
    """The squash reproduces the unsquashed chain's seeds, and is a deployed no-op."""

    def setUp(self) -> None:
        # Restore core to head after every case regardless of outcome — these cases
        # drive the shared connection to zero and back, and must not leave it off-head
        # for the rest of the (xdist-reused) worker DB.
        self.addCleanup(self._restore_core_head)

    @staticmethod
    def _applied_core() -> set[str]:
        return {name for app, name in MigrationRecorder(connection).applied_migrations() if app == "core"}

    @staticmethod
    def _reset_core_to_zero() -> None:
        """Unapply core to an empty schema and clear every core ``django_migrations`` row.

        The explicit record wipe matters: after the unsquashed-chain apply the executor's
        ``check_replacements`` also records the squash, so the individual rows and the
        squash row coexist. ``migrate zero`` (squash graph) drops the schema but leaves
        the individual rows behind; wiping them gives the next phase a clean slate.
        """
        connection.close()
        call_command("migrate", "core", "zero", "--no-input", verbosity=0)
        MigrationRecorder(connection).migration_qs.filter(app="core").delete()

    def _restore_core_head(self) -> None:
        self._reset_core_to_zero()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _apply_unsquashed_chain() -> None:
        """Apply the individual ``0001..0030`` migrations, bypassing the squash's replaces."""
        connection.close()
        executor = MigrationExecutor(connection)
        # Build the graph with the squash's ``replaces`` NOT applied, so the individual
        # 0001..0030 nodes stay live. Set the flag before ``build_graph`` (deferred via
        # ``load=False``) rather than the ``replace_migrations`` kwarg the stubs lack.
        loader = MigrationLoader(connection, load=False)
        loader.replace_migrations = False
        loader.build_graph()
        executor.loader = loader
        executor.migrate([("core", _LAST_REPLACED)])

    def test_fresh_install_squashed_matches_unsquashed_chain(self) -> None:
        """(a) Fresh install through the squash == fresh install through the raw chain."""
        self._reset_core_to_zero()
        self._apply_unsquashed_chain()
        unsquashed = _snapshot()

        self._reset_core_to_zero()
        call_command("migrate", "core", "--no-input", verbosity=0)  # applies the squash
        squashed = _snapshot()

        # Anti-vacuity: the seeds are present, so the equality below compares real data.
        assert len(squashed["Loop"]) > 0
        assert "arch_review" in squashed["Prompt"]
        assert "offline" in squashed["Mode"]

        assert squashed == unsquashed

    def test_deployed_db_at_0030_applies_squash_as_pure_noop(self) -> None:
        """(b) A DB at 0030 via the raw chain records the squash via replaces, runs nothing."""
        self._reset_core_to_zero()
        self._apply_unsquashed_chain()
        # Simulate the real deployed pre-state: at 0030 via the individual chain, the
        # squash file present but NOT yet recorded applied.
        MigrationRecorder(connection).migration_qs.filter(app="core", name=_SQUASH).delete()

        applied_before = self._applied_core()
        assert _LAST_REPLACED in applied_before
        assert _SQUASH not in applied_before
        before = _snapshot()

        call_command("migrate", "core", "--no-input", verbosity=0)

        applied_after = self._applied_core()
        assert _SQUASH in applied_after  # recorded applied via replaces, ops never ran
        assert _snapshot() == before  # seeded data untouched
