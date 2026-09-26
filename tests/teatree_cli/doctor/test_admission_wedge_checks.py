"""Doctor must SEE the wedge it already has a check for (#4201).

`_check_worker_memory_cap` existed and would have flagged the under-sized cap on
a deployed worker — it never fired, because it reads ``TEATREE_ROLE`` while that
deployment's compose names its role variable differently. A role-aware check that cannot resolve its
own role is not conservative, it is INERT: it returns OK for every container it was
written to guard.
"""

import os
from pathlib import Path

import pytest

from teatree.cli.doctor.checks_resources import _check_resume_ceiling_reachable, _check_worker_memory_cap
from teatree.utils.ram_scope import AGENT_WORKLOAD_FLOOR_ENV


@pytest.fixture(autouse=True)
def _no_ambient_role_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every ambient ``*_ROLE`` var so each test owns the whole role namespace.

    The alias scan reads the real environment, and this suite runs ON a deployed worker
    whose own ``*_ROLE`` alias is exactly what it is written to find — without this the
    negative cases assert against a role the test never set.
    """
    for name in list(os.environ):
        if name.endswith("_ROLE"):
            monkeypatch.delenv(name, raising=False)


def _cap(tmp_path: Path, gib: float) -> Path:
    path = tmp_path / "memory.max"
    path.write_text(str(int(gib * 1024**3)), encoding="utf-8")
    return path


class TestRoleResolutionIsNotDeploymentSpecific:
    """The role env var differs per deployment; the check must not silently miss one."""

    def test_an_aliased_role_worker_with_a_low_cap_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The measured deployed shape: an aliased *_ROLE var set, TEATREE_ROLE absent.
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        assert not _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")

    def test_a_teatree_role_worker_still_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_ROLE", "worker")
        monkeypatch.delenv("DEPLOY_ROLE", raising=False)
        assert not _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")

    def test_a_non_worker_role_stays_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("DEPLOY_ROLE", "admin")
        assert _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")

    def test_a_roomy_cap_stays_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        assert _check_worker_memory_cap(v2=_cap(tmp_path, 16), v1=tmp_path / "absent")


class TestImpossibleResumeCeilingIsAHardFail:
    """A cap under the resume floor can never re-admit — doctor must refuse it."""

    def test_the_measured_five_gib_cap_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        assert not _check_resume_ceiling_reachable(v2=_cap(tmp_path, 5), v1=tmp_path / "absent")

    def test_a_cap_above_the_resume_floor_is_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        assert _check_resume_ceiling_reachable(v2=_cap(tmp_path, 12), v1=tmp_path / "absent")

    def test_an_uncapped_container_is_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        uncapped = tmp_path / "memory.max"
        uncapped.write_text("max", encoding="utf-8")
        assert _check_resume_ceiling_reachable(v2=uncapped, v1=tmp_path / "absent")

    def test_a_non_worker_role_is_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("DEPLOY_ROLE", "admin")
        assert _check_resume_ceiling_reachable(v2=_cap(tmp_path, 5), v1=tmp_path / "absent")


class TestTheTwoMemoryGatesDoNotOverlap:
    """#151: exactly ONE gate fires per broken cap, and it is the one that owns the fault.

    Two different questions. ``_check_worker_memory_cap`` asks "is there room to work at
    all?"; ``_check_resume_ceiling_reachable`` asks "can the brake ever release?". A cap
    too small to be BOX-SCOPED belongs to the first: the governor drops that cgroup's
    arithmetic and reads the host instead, so no brake becomes permanent and naming an
    impossible ceiling would diagnose the wrong container.

    The predicate has no lower bound of its own — it takes a scope-qualified cap. Closing
    its band at a restated DEFAULT floor instead is what #151 caught: that floor is
    operator-overridable, so a lowered override made a small cap box-scoped and braked
    forever while the predicate silently declined to name it.
    """

    def test_a_sub_floor_cap_is_reported_as_a_broken_cap_not_an_impossible_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        monkeypatch.delenv(AGENT_WORKLOAD_FLOOR_ENV, raising=False)
        cap = _cap(tmp_path, 3)

        assert not _check_worker_memory_cap(v2=cap, v1=tmp_path / "absent")
        assert _check_resume_ceiling_reachable(v2=cap, v1=tmp_path / "absent")

    def test_a_cap_inside_the_band_still_reports_the_impossible_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other direction: scope-qualifying must not silence the fault it exists for."""
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        monkeypatch.delenv(AGENT_WORKLOAD_FLOOR_ENV, raising=False)
        cap = _cap(tmp_path, 5)

        assert _check_worker_memory_cap(v2=cap, v1=tmp_path / "absent")
        assert not _check_resume_ceiling_reachable(v2=cap, v1=tmp_path / "absent")

    def test_a_lowered_floor_pulls_a_small_cap_back_into_both_gates_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The override case, RED before #151.

        At floor 2 a 3 GiB cap IS box-scoped, so the cgroup governs and the lane is braked
        below a 6 GiB resume floor it can never reach — and the predicate must say so. It
        did not: its lower bound was the DEFAULT 4 GiB, not the effective floor.
        """
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        monkeypatch.setenv(AGENT_WORKLOAD_FLOOR_ENV, "2")
        cap = _cap(tmp_path, 3)

        assert _check_worker_memory_cap(v2=cap, v1=tmp_path / "absent"), "3 GiB clears a floor of 2"
        assert not _check_resume_ceiling_reachable(v2=cap, v1=tmp_path / "absent")


class TestAliasScanIsNotFooledByUnrelatedVars:
    """A `*_ROLE` var whose value is not a known role must not be read as one."""

    def test_an_unrelated_role_shaped_var_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("AWS_IAM_ROLE", "arn:aws:iam::1234:role/something")
        # Not a known role, so no role resolves and the worker check stays OK.
        assert _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")

    def test_the_canonical_var_wins_over_an_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_ROLE", "admin")
        monkeypatch.setenv("DEPLOY_ROLE", "worker")
        assert _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")


class TestConflictingRoleAliasesAreLoudNotArbitrary:
    """Two aliases naming DIFFERENT roles is unreadable input — never a silent pick.

    Resolving by sorted-order-wins turns an ambiguous environment into a confident
    wrong answer: ``IAM_ROLE=admin`` sorts before ``STACK_ROLE=worker``, so the
    resolver reports "admin" on a worker and both hard gates go inert — the exact
    failure this check exists to catch. A resolver that cannot answer its own
    question must not return the same value as one that answered "not the worker".
    """

    def test_two_aliases_naming_different_roles_fail_the_memory_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("IAM_ROLE", "admin")
        monkeypatch.setenv("STACK_ROLE", "worker")
        assert not _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")

    def test_two_aliases_naming_different_roles_fail_the_resume_ceiling_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("IAM_ROLE", "admin")
        monkeypatch.setenv("STACK_ROLE", "worker")
        assert not _check_resume_ceiling_reachable(v2=_cap(tmp_path, 5), v1=tmp_path / "absent")

    def test_a_conflict_between_two_non_worker_roles_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Neither candidate is the worker, but the environment is still unreadable —
        # nothing licenses trusting the set of candidates to be complete.
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("IAM_ROLE", "admin")
        monkeypatch.setenv("STACK_ROLE", "watchdog")
        assert not _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")

    def test_the_failure_names_both_conflicting_variables_and_the_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("IAM_ROLE", "admin")
        monkeypatch.setenv("STACK_ROLE", "worker")
        _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")
        out = capsys.readouterr().out
        assert "IAM_ROLE" in out
        assert "STACK_ROLE" in out
        assert "TEATREE_ROLE" in out

    def test_aliases_that_agree_are_not_a_conflict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)
        monkeypatch.setenv("DEPLOY_ROLE", "admin")
        monkeypatch.setenv("STACK_ROLE", "admin")
        assert _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")

    def test_the_canonical_var_settles_a_conflict_without_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TEATREE_ROLE is authoritative, so a disagreeing alias pair is moot beside it.
        monkeypatch.setenv("TEATREE_ROLE", "worker")
        monkeypatch.setenv("IAM_ROLE", "admin")
        monkeypatch.setenv("STACK_ROLE", "watchdog")
        assert not _check_worker_memory_cap(v2=_cap(tmp_path, 2), v1=tmp_path / "absent")
        assert _check_worker_memory_cap(v2=_cap(tmp_path, 16), v1=tmp_path / "absent")

    def test_an_explicit_role_argument_settles_a_conflict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IAM_ROLE", "admin")
        monkeypatch.setenv("STACK_ROLE", "worker")
        assert _check_worker_memory_cap(role="admin", v2=_cap(tmp_path, 2), v1=tmp_path / "absent")
