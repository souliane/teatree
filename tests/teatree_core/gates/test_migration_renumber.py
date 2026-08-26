"""The renumbered-migration detector (souliane/teatree#4591).

``django-linear-migrations`` enforces one leaf, so a long-lived branch MUST
renumber its migration on rebase. Django records applied migrations by NAME, so
the rename strands every DB that applied the old number: the schema change is
present, the new name reads as pending, ``migrate`` re-runs it and dies on the
object it just found. The evidence is all in ``django_migrations``.
"""

from teatree.core.gates.migration_renumber import (
    COLLISION_MARKERS,
    RenumberedMigration,
    looks_like_a_collision,
    match_renumbered,
    renumber_hint,
)

APPLIED = (("core", "0054_worktree_occupancy_claim", "2026-08-03"),)
PENDING = ("core.0080_worktree_occupancy_claim",)
ON_DISK = frozenset({("core", "0080_worktree_occupancy_claim")})


class TestMatchRenumbered:
    def test_the_reproduced_case_is_detected(self) -> None:
        pairs = match_renumbered(pending=PENDING, applied=APPLIED, on_disk=ON_DISK)

        assert pairs == [
            RenumberedMigration(
                app="core",
                applied_name="0054_worktree_occupancy_claim",
                pending_name="0080_worktree_occupancy_claim",
                applied_at="2026-08-03",
            )
        ]

    def test_an_old_name_still_on_disk_is_not_a_renumber(self) -> None:
        """Two migrations that both exist are two migrations, however alike their names."""
        on_disk = ON_DISK | {("core", "0054_worktree_occupancy_claim")}

        assert match_renumbered(pending=PENDING, applied=APPLIED, on_disk=on_disk) == []

    def test_a_different_suffix_is_not_a_renumber(self) -> None:
        applied = (("core", "0054_something_else", "2026-08-03"),)

        assert match_renumbered(pending=PENDING, applied=applied, on_disk=ON_DISK) == []

    def test_another_apps_row_is_not_a_renumber(self) -> None:
        applied = (("other", "0054_worktree_occupancy_claim", "2026-08-03"),)

        assert match_renumbered(pending=PENDING, applied=applied, on_disk=ON_DISK) == []

    def test_the_same_number_is_not_a_renumber(self) -> None:
        applied = (("core", "0080_worktree_occupancy_claim", "2026-08-03"),)

        assert match_renumbered(pending=PENDING, applied=applied, on_disk=ON_DISK) == []

    def test_an_unnumbered_name_is_ignored_rather_than_split(self) -> None:
        assert match_renumbered(pending=("core.custom",), applied=(("core", "custom", ""),), on_disk=frozenset()) == []

    def test_renumbered_twice_reports_every_stale_row_in_a_stable_order(self) -> None:
        applied = (
            ("core", "0070_worktree_occupancy_claim", "2026-08-10"),
            ("core", "0054_worktree_occupancy_claim", "2026-08-03"),
        )

        pairs = match_renumbered(pending=PENDING, applied=applied, on_disk=ON_DISK)

        assert [pair.applied_name for pair in pairs] == [
            "0054_worktree_occupancy_claim",
            "0070_worktree_occupancy_claim",
        ]


class TestLooksLikeACollision:
    def test_the_duplicate_column_failure_is_a_collision(self) -> None:
        assert looks_like_a_collision("OperationalError: duplicate column name: occupancy_expires_at")

    def test_a_missing_table_is_not_a_collision(self) -> None:
        assert not looks_like_a_collision("OperationalError: no such table: teatree_merge_clear")

    def test_every_marker_is_matched_case_insensitively(self) -> None:
        assert all(looks_like_a_collision(f"psycopg.errors: {marker.upper()}") for marker in COLLISION_MARKERS)


class TestRenumberHint:
    def test_it_names_both_numbers_the_evidence_and_the_reconcile(self) -> None:
        hint = renumber_hint(match_renumbered(pending=PENDING, applied=APPLIED, on_disk=ON_DISK))

        assert "core.0080_worktree_occupancy_claim" in hint
        assert "core.0054_worktree_occupancy_claim" in hint
        assert "2026-08-03" in hint
        assert "t3 teatree db reconcile-renumbered --apply" in hint

    def test_no_pairs_means_no_hint(self) -> None:
        assert renumber_hint([]) == ""
