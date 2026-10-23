"""Per-test mutant kills identify redundancy without treating missing evidence as a kill."""

import pytest

from teatree.quality.kill_matrix import KillMatrix, KillMatrixError, KillRow, build_kill_matrix
from teatree.quality.mutation_run import MutationResult


def test_subset_kills_mark_only_later_test_redundant() -> None:
    results = {
        "tests/test_x.py::test_broad": MutationResult(("m1", "m2"), (), ()),
        "tests/test_x.py::test_narrow": MutationResult(("m1",), ("m2",), ()),
    }
    matrix = build_kill_matrix(
        ("tests/test_x.py::test_broad", "tests/test_x.py::test_narrow"),
        ("src/pkg.py",),
        run_test=lambda node, _: results[node],
    )
    assert matrix.rows[0].redundant_with == ()
    assert matrix.rows[1].redundant_with == ("tests/test_x.py::test_broad",)
    assert matrix.head_sha == ""


def test_zero_kills_and_inconclusive_are_not_falsely_reported_as_redundant() -> None:
    results = {
        "tests/test_x.py::test_a": MutationResult((), ("m1",), ()),
        "tests/test_x.py::test_b": MutationResult((), (), ("m1",)),
    }
    matrix = build_kill_matrix(tuple(results), ("src/pkg.py",), run_test=lambda node, _: results[node])
    assert matrix.rows[0].zero_kills
    assert not matrix.rows[1].measured
    assert matrix.rows[1].redundant_with == ()
    assert "ZERO_KILLS" in matrix.render()
    assert "UNMEASURED" in matrix.render()


def test_head_bound_matrix_rejects_other_head() -> None:
    matrix = KillMatrix("a" * 40, (KillRow("tests/test_x.py::test_a", ("m1",), (), (), ()),))
    assert matrix.for_head("b" * 40) is None
    assert matrix.for_head("a" * 40) is matrix


def test_no_generated_mutants_is_not_a_measured_row() -> None:
    with pytest.raises(KillMatrixError, match="classified no mutants"):
        build_kill_matrix(
            ("tests/test_x.py::test_a",),
            ("src/pkg.py",),
            run_test=lambda _node, _modules: MutationResult((), (), ()),
        )
