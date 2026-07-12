"""Fleet-safety gate: a fresh DB AND an old-chain DB both migrate clean (#3072).

The core migrations were squashed (#3072): the old ``0001..0043`` chain was
collapsed into a single ``0001_initial`` that was MODIFIED in place — it keeps the
name ``0001_initial`` and carries no ``replaces``. A live teatree install that
already ran the old chain has ``django_migrations`` rows for
``(core, 0001_initial)`` .. ``(core, 0043_enable_sound_default_loops)``.

The fleet-critical question: does ``migrate`` on the squashed code brick such an
install by re-running ``0001``'s ``CreateModel`` operations against tables that
already exist? It does not — and the reason is the name reuse, not ``replaces``.
Django's executor keys applied state on ``(app_label, name)``. Because the squash
kept the name ``0001_initial``, the recorded ``(core, 0001_initial)`` row marks
the new squashed node applied (a no-op); the executor never re-runs its
``CreateModel`` ops. The two post-squash ``AddField`` migrations (``0002``/``0003``)
then apply cleanly onto the already-present ``teatree_implemented_issue_marker``
table.

This gate proves both paths empirically and is anti-vacuous:
``test_old_chain_db_bricks_when_initial_record_name_does_not_match`` shows the
executor DOES brick with ``CREATE``-existing-table the moment the recorded
initial name stops matching the squashed node — the exact regression a
name-changing re-squash (or a future ``0001`` rename) would reintroduce.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.db.utils import OperationalError
from django.test import TransactionTestCase

from tests.teatree_core._migration_graph import core_migration_names

_MARKER_TABLE = "teatree_implemented_issue_marker"

# The columns the post-squash ``0002``/``0003`` AddFields introduce — absent from
# any old-chain (0043) install, added cleanly onto the existing marker table.
_CLAIMED_BY_INSTANCE = "claimed_by_instance"
_CLAIM_REF_SHA = "claim_ref_sha"

# The current, post-squash core graph — every real on-disk ``core`` migration,
# derived from disk via the shared ``_migration_graph`` seam (never a hardcoded set)
# so it cannot fall out of sync when a new migration lands. ``_restore_core_head``
# relies on this being the FULL real graph to tell real records (keep) from the
# fileless pre-squash phantom rows (delete): a stale set silently deletes the real
# records for the migrations it omits and then re-applies their ops onto
# already-present schema — a ``CreateModel`` "table already exists" brick that
# corrupts the xdist-reused ``default`` DB for every sibling test (the #3159 preset
# ``CreateModel`` migration first exposed this latent staleness; AddFields tolerated
# it). ``test_schema_guard`` derives its ledger names from the same shared seam.
_CURRENT_GRAPH: frozenset[str] = frozenset(core_migration_names())

# The frozen pre-squash migration names (post-``0001_initial``) as they existed at
# ``666a9730^`` right before the squash. A fully-updated old install recorded
# exactly these in ``django_migrations``; the squash deleted the files but the
# rows survive as fileless history. Frozen — these names never change.
_PRE_SQUASH_CHAIN: tuple[str, ...] = (
    "0002_taskattempt_error_fingerprint_taskattempt_iteration",
    "0003_instructioncompliancesnapshot_and_more",
    "0004_task_created_at_task_subject",
    "0005_backfill_task_subject",
    "0006_delete_miniloopmarker",
    "0007_deferredquestion_parked_task",
    "0008_trustedidentity",
    "0009_seed_loop_descriptions",
    "0010_worktree_compose_project",
    "0011_looplease_generation",
    "0012_anthropictokenusage_anthropicactivepick",
    "0014_ticket_repo_namespaced_key",
    "0015_agent_harness_two_layer_config",
    "0016_loop_colleague_facing",
    "0017_taskattempt_lane",
    "0018_projectlearning",
    "0019_mrreviewlock",
    "0020_ticket_expedited",
    "0021_review_evidence",
    "0022_transport_verify_and_dead_letter",
    "0023_attachment_manifest",
    "0024_merge_expedite_waiver",
    "0025_knownissue",
    "0026_waitingitem",
    "0027_standinggoal",
    "0028_mergeaudit_repo_slug",
    "0029_planartifact_base_sha_adequacy",
    "0030_factoryscoresnapshot",
    "0031_criticfinding_verdict_dispatch",
    "0032_outer_loop_experiment",
    "0033_looplease_driver",
    "0034_directive",
    "0035_seed_directive_loop",
    "0036_reprowaiver_reproevidence",
    "0037_directive_taint_incomingevent_provenance",
    "0038_usagewindowstate_task_not_before",
    "0039_consolidate_critic_flags_delete_ambient",
    "0040_keep_pending_state",
    "0041_send_audit",
    "0042_alter_deferredquestion_resolved_via",
    "0043_enable_sound_default_loops",
)


# ``setUp`` and every case reverse-migrate ``core`` to ``zero`` and re-apply on the
# shared ``default`` connection — several seconds single-core, exceeding the global
# 60s ``pytest-timeout`` under ``-n auto --cov`` contention. Scoped 240s bump
# mirrors the sibling migrate test; the global 60s stays the hang-detector (#1189).
@pytest.mark.timeout(240)
class TestSquashMigratesCleanBothWays(TransactionTestCase):
    """A fresh DB and a recorded-old-chain DB both migrate to head with no brick."""

    def setUp(self) -> None:
        # Restore core to head after every case regardless of how it ended — a
        # deliberately-bricked case must not leave the shared connection off-head
        # for the rest of the session.
        self.addCleanup(self._restore_core_head)

    def _restore_core_head(self) -> None:
        connection.close()  # discard any aborted-transaction state from a bricked case
        recorder = MigrationRecorder(connection)
        # Re-record the squashed initial so a mid-brick partial state rolls to head
        # without re-running its CreateModel ops, then drop the fileless phantom
        # rows so django_migrations matches the real graph again.
        recorder.record_applied("core", "0001_initial")
        recorder.migration_qs.filter(app="core").exclude(name__in=_CURRENT_GRAPH).delete()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _record_pre_squash_chain() -> None:
        recorder = MigrationRecorder(connection)
        for name in _PRE_SQUASH_CHAIN:
            recorder.record_applied("core", name)

    @staticmethod
    def _applied_core() -> set[str]:
        return {name for app, name in MigrationRecorder(connection).applied_migrations() if app == "core"}

    @staticmethod
    def _marker_columns() -> set[str]:
        with connection.cursor() as cursor:
            return {row[1] for row in cursor.execute(f"PRAGMA table_info({_MARKER_TABLE})").fetchall()}

    def _reset_to_old_chain_install(self) -> None:
        """Reproduce an install that ran the full pre-squash chain.

        Applying only the squashed ``0001_initial`` builds exactly the old-chain
        end-state schema — the squash is behavior-preserving, so its output equals
        the cumulative old 0001..0043 schema (pinned by
        ``test_initial_migration_seed``) — and stops before the post-squash
        ``0002``/``0003`` AddFields, so the marker table lacks the fleet columns
        just like a real 0043 install. The frozen pre-squash names are then
        recorded so ``django_migrations`` mirrors the real fileless history.
        """
        call_command("migrate", "core", "zero", "--no-input", verbosity=0)
        call_command("migrate", "core", "0001_initial", "--no-input", verbosity=0)
        self._record_pre_squash_chain()

    def test_fresh_empty_db_migrates_from_zero(self) -> None:
        """Path (a): a brand-new empty DB applies the whole squashed graph cleanly."""
        call_command("migrate", "core", "zero", "--no-input", verbosity=0)
        call_command("migrate", "core", "--no-input", verbosity=0)

        assert self._applied_core() >= _CURRENT_GRAPH
        columns = self._marker_columns()
        assert _CLAIMED_BY_INSTANCE in columns
        assert _CLAIM_REF_SHA in columns

    def test_old_chain_db_migrates_clean(self) -> None:
        """Path (b): a DB carrying the recorded old chain migrates without a brick.

        The new ``migrate`` must skip the squashed ``0001_initial`` (its name is
        already recorded) and apply only the two post-squash AddFields onto the
        existing marker table — no ``CREATE``-existing-table, no lost data.
        """
        self._reset_to_old_chain_install()
        assert _CLAIMED_BY_INSTANCE not in self._marker_columns()  # 0043 install lacks the fleet columns

        # Must not raise OperationalError("table already exists" / "duplicate column").
        call_command("migrate", "core", "--no-input", verbosity=0)

        applied = self._applied_core()
        assert "0001_initial" in applied  # treated as applied — never re-created
        assert "0002_implementedissuemarker_claimed_by_instance" in applied
        assert "0003_implementedissuemarker_claim_ref_sha" in applied
        columns = self._marker_columns()
        assert _CLAIMED_BY_INSTANCE in columns
        assert _CLAIM_REF_SHA in columns

    def test_old_chain_db_bricks_when_initial_record_name_does_not_match(self) -> None:
        """Anti-vacuity: the clean path holds ONLY because the recorded name matches.

        Drop the recorded ``(core, 0001_initial)`` row (as a name-changing re-squash
        or a future ``0001`` rename would) and the executor re-runs ``0001``'s
        ``CreateModel`` against tables that already exist — the exact fleet brick.
        Proves the clean-path assertion above is not vacuous.
        """
        self._reset_to_old_chain_install()
        MigrationRecorder(connection).migration_qs.filter(app="core", name="0001_initial").delete()

        with pytest.raises(OperationalError):
            call_command("migrate", "core", "--no-input", verbosity=0)
