"""Schema-vs-code readiness, the deploy-order predicate the hot pull never had (#3901).

`self_update` fast-forwards the running install's HEAD on a cadence while the
schema migrate is a separate step the `init` container runs at boot. Between a
merge landing and the next restart a live worker can execute code whose models
the control DB does not carry. These pin the three-valued verdict that closes
that window, its TTL memo (the claim path reads it, so it must not re-walk the
migration graph per claim), and the kill switch.

The real-DB cases drive a private, file-backed SQLite alias (the #2915 rule) so
a reverse-migrate can only corrupt a throwaway file, never the shared
xdist-worker `default` database.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from teatree.core.schema_readiness import (
    READINESS_TTL_SECONDS,
    SchemaReadiness,
    SchemaState,
    invalidate_schema_readiness,
    read_schema_readiness,
    schema_admission_block_reason,
)
from tests.teatree_core._migration_graph import core_head_migration
from tests.teatree_core.conftest import SchemaGuardAlias

_PROBE = "teatree.core.schema_readiness.pending_migrations"


@pytest.fixture(autouse=True)
def _clear_memo() -> Iterator[None]:
    invalidate_schema_readiness()
    yield
    invalidate_schema_readiness()


# The two real-walk cases each run a full `migrate` against a throwaway alias, which
# exceeds the global 60s timeout under `-n auto --cov` contention (the #1189 bump the
# sibling schema_guard suite takes for the same reason).
@pytest.mark.timeout(240)
class TestReadSchemaReadiness:
    """The three-valued verdict over the live control DB.

    The real-walk cases need no ``django_db`` marker: ``schema_guard_alias`` lifts the
    DB guard itself and never touches the shared ``default`` connection (#2915).
    """

    @pytest.mark.real_schema_readiness
    def test_current_schema_admits_work(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.register_current()

        readiness = read_schema_readiness(alias)

        assert readiness.state is SchemaState.CURRENT
        assert readiness.pending == ()
        assert readiness.admits_work is True
        assert readiness.block_reason() == ""

    @pytest.mark.real_schema_readiness
    def test_behind_schema_names_every_pending_migration(self, schema_guard_alias: SchemaGuardAlias) -> None:
        alias = schema_guard_alias.make_stale()

        readiness = read_schema_readiness(alias)

        assert readiness.state is SchemaState.BEHIND
        assert f"core.{core_head_migration()}" in readiness.pending
        assert readiness.admits_work is False
        assert "db migrate" in readiness.block_reason()

    def test_probe_failure_is_unknown_not_current(self) -> None:
        """A probe that RAISED must never read as a current schema (fail closed)."""
        with patch(_PROBE, side_effect=RuntimeError("graph exploded")):
            readiness = read_schema_readiness("default")

        assert readiness.state is SchemaState.UNKNOWN
        assert readiness.admits_work is False
        assert "graph exploded" in readiness.detail
        assert readiness.block_reason() != ""


class TestAdmissionBlockReason:
    """The claim-path face: `""` admits, anything else is the refusal text."""

    def test_current_admits(self) -> None:
        with patch(_PROBE, return_value=[]):
            assert schema_admission_block_reason("default") == ""

    def test_behind_blocks_naming_the_migrations(self) -> None:
        with patch(_PROBE, return_value=["core.0042_widget"]):
            reason = schema_admission_block_reason("default")

        assert "core.0042_widget" in reason

    def test_unknown_blocks(self) -> None:
        with patch(_PROBE, side_effect=RuntimeError("boom")):
            assert schema_admission_block_reason("default") != ""

    def test_kill_switch_off_never_blocks(self) -> None:
        """`schema_readiness_gate_enabled=false` is the never-lockout escape."""
        with (
            patch(_PROBE, return_value=["core.0042_widget"]),
            patch(
                "teatree.core.schema_readiness.schema_readiness_gate_enabled",
                return_value=False,
            ),
        ):
            assert schema_admission_block_reason("default") == ""

    def test_setting_read_failure_keeps_the_gate_enabled(self) -> None:
        """An unreadable kill switch fails CLOSED — it cannot silently open the gate."""
        with (
            patch(_PROBE, return_value=["core.0042_widget"]),
            patch(
                "teatree.core.schema_readiness.get_effective_settings",
                side_effect=RuntimeError("config store down"),
            ),
        ):
            assert schema_admission_block_reason("default") != ""


class TestReadinessMemo:
    """The TTL memo — the claim path must not re-walk the migration graph per claim."""

    def test_second_read_within_the_ttl_does_not_reprobe(self) -> None:
        with patch(_PROBE, return_value=[]) as probe:
            first = schema_admission_block_reason("default")
            second = schema_admission_block_reason("default")

        assert (first, second) == ("", "")
        assert probe.call_count == 1

    def test_invalidate_forces_a_reprobe(self) -> None:
        """The hot-pull seam: code moved on disk, so the cached verdict is void."""
        with patch(_PROBE, return_value=[]) as probe:
            schema_admission_block_reason("default")
            invalidate_schema_readiness()
            schema_admission_block_reason("default")

        assert probe.call_count == 2

    def test_expired_entry_is_reprobed(self) -> None:
        clock = [100.0]
        with (
            patch(_PROBE, return_value=[]) as probe,
            patch("teatree.core.schema_readiness.monotonic", lambda: clock[0]),
        ):
            schema_admission_block_reason("default")
            clock[0] += READINESS_TTL_SECONDS + 1.0
            schema_admission_block_reason("default")

        assert probe.call_count == 2

    def test_memo_is_per_alias(self) -> None:
        with patch(_PROBE, return_value=[]) as probe:
            schema_admission_block_reason("default")
            schema_admission_block_reason("other")

        assert probe.call_count == 2


class TestReadinessValue:
    """`SchemaReadiness` is the whole contract — no ambiguous empty."""

    def test_unknown_with_no_pending_still_refuses(self) -> None:
        """An empty `pending` tuple means "none listed", never "the schema is fine"."""
        readiness = SchemaReadiness(state=SchemaState.UNKNOWN, detail="probe unavailable")

        assert readiness.pending == ()
        assert readiness.admits_work is False
