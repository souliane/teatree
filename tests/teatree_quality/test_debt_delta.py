"""The pure debt-delta scanner + plan-manifest waiver logic (north-star PR-3).

``scan_debt_delta`` reads a unified diff and returns every NET-NEW tech-debt
suppression it introduces — a new ``# noqa`` / ``# type: ignore`` /
``# pragma: no cover``, an unreferenced ``pytest.mark.skip`` / ``xfail``, a new
``per-file-ignores`` entry, or a lowered coverage ``fail_under`` floor. It is the
ship-chain sibling of ``gate_relaxation`` and reuses its ``parse_diff`` +
``blank_string_literals`` primitives.

Anti-vacuity is proved BOTH DIRECTIONS per signal (the design's contract): each
pattern is proven to FIRE on an added line and proven NOT to fire when the same
suppression is REMOVED (a ``-`` line) or already present as unchanged context —
that "delta, not absolute" property is what makes legacy debt exempt and the
gate a shrink-only ratchet. The waiver tests prove an audited plan-manifest
``approved_debt`` entry lets a genuinely-justified suppression through while a
blank-reason waiver covers nothing.
"""

from teatree.core.models.types import ApprovedDebt
from teatree.quality.debt_delta import (
    DebtIntroduction,
    DebtWaiver,
    load_debt_waivers,
    scan_debt_delta,
    unwaived_debt,
    waiver_covers,
)


def _diff(
    path: str,
    *,
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    context: tuple[str, ...] = (),
) -> str:
    """A minimal unified diff for one *path* (git ``a/``/``b/`` prefixes)."""
    lines = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,3 +1,3 @@"]
    lines += [f" {c}" for c in context]
    lines += [f"-{r}" for r in removed]
    lines += [f"+{a}" for a in added]
    return "\n".join(lines) + "\n"


def _kinds(diff: str) -> list[str]:
    return sorted(intro.kind for intro in scan_debt_delta(diff))


class TestNoqaSignal:
    def test_added_noqa_fires(self) -> None:
        diff = _diff("src/teatree/m.py", added=("x = frobnicate()  # noqa: F821",))
        assert _kinds(diff) == ["noqa"]

    def test_removed_noqa_does_not_fire(self) -> None:
        # Delta, not absolute: DELETING a suppression is a shrink, never a finding.
        diff = _diff("src/teatree/m.py", removed=("x = frobnicate()  # noqa: F821",))
        assert scan_debt_delta(diff) == []

    def test_unchanged_context_noqa_does_not_fire(self) -> None:
        # Pre-existing (legacy) debt on a context line is exempt — never in the
        # added set, so a diff that only surrounds it stays clean.
        diff = _diff(
            "src/teatree/m.py",
            context=("legacy = compute()  # noqa: E501",),
            added=("brand_new = value",),
        )
        assert scan_debt_delta(diff) == []

    def test_noqa_inside_a_string_literal_does_not_fire(self) -> None:
        diff = _diff("src/teatree/m.py", added=('marker = "value  # noqa: F821"',))
        assert scan_debt_delta(diff) == []

    def test_prose_comment_mentioning_noqa_does_not_fire(self) -> None:
        diff = _diff("src/teatree/m.py", added=("value = compute()  # we removed the old noqa here",))
        assert scan_debt_delta(diff) == []


class TestTypeIgnoreSignal:
    def test_added_type_ignore_fires(self) -> None:
        diff = _diff("src/teatree/m.py", added=("row = obj.fk  # type: ignore[attr-defined]",))
        assert _kinds(diff) == ["type_ignore"]

    def test_removed_type_ignore_does_not_fire(self) -> None:
        diff = _diff("src/teatree/m.py", removed=("row = obj.fk  # type: ignore[attr-defined]",))
        assert scan_debt_delta(diff) == []

    def test_type_ignore_inside_a_string_does_not_fire(self) -> None:
        diff = _diff("src/teatree/m.py", added=('doc = "use # type: ignore sparingly"',))
        assert scan_debt_delta(diff) == []


class TestPragmaNoCoverSignal:
    def test_added_pragma_no_cover_fires(self) -> None:
        diff = _diff("src/teatree/m.py", added=("def unreachable():  # pragma: no cover",))
        assert _kinds(diff) == ["pragma_no_cover"]

    def test_removed_pragma_no_cover_does_not_fire(self) -> None:
        diff = _diff("src/teatree/m.py", removed=("def unreachable():  # pragma: no cover",))
        assert scan_debt_delta(diff) == []


class TestTestSkipSignal:
    def test_added_unreferenced_skip_fires(self) -> None:
        diff = _diff("tests/teatree/test_m.py", added=("@pytest.mark.skip(reason='flaky')",))
        assert _kinds(diff) == ["test_skip"]

    def test_added_unreferenced_xfail_fires(self) -> None:
        diff = _diff("tests/teatree/test_m.py", added=("@pytest.mark.xfail",))
        assert _kinds(diff) == ["test_skip"]

    def test_skip_with_a_ticket_reference_is_allowed(self) -> None:
        # A skip that names a tracking ticket is tracked debt, not silent debt.
        diff = _diff(
            "tests/teatree/test_m.py",
            added=("@pytest.mark.skip(reason='blocked by #4242 upstream fix')",),
        )
        assert scan_debt_delta(diff) == []

    def test_removed_skip_does_not_fire(self) -> None:
        diff = _diff("tests/teatree/test_m.py", removed=("@pytest.mark.skip(reason='flaky')",))
        assert scan_debt_delta(diff) == []


class TestPerFileIgnoreSignal:
    def test_added_per_file_ignores_header_fires(self) -> None:
        diff = _diff("pyproject.toml", added=("[tool.ruff.lint.per-file-ignores]",))
        assert _kinds(diff) == ["per_file_ignore"]

    def test_added_glob_to_codes_entry_fires(self) -> None:
        # An entry added under an EXISTING per-file-ignores table (no header line
        # in the diff) is caught by its glob->ruff-codes shape.
        diff = _diff("pyproject.toml", added=('"src/teatree/legacy/*.py" = ["PLR0913", "C901"]',))
        assert _kinds(diff) == ["per_file_ignore"]

    def test_removed_per_file_ignores_entry_does_not_fire(self) -> None:
        diff = _diff("pyproject.toml", removed=('"src/teatree/legacy/*.py" = ["PLR0913"]',))
        assert scan_debt_delta(diff) == []

    def test_unrelated_toml_list_assignment_does_not_fire(self) -> None:
        diff = _diff("pyproject.toml", added=('dependencies = ["httpx", "django"]',))
        assert scan_debt_delta(diff) == []

    def test_per_file_ignores_only_scanned_in_config_files(self) -> None:
        diff = _diff("src/teatree/m.py", added=('config = "per-file-ignores"',))
        assert scan_debt_delta(diff) == []


class TestCoverageFloorSignal:
    def test_lowered_fail_under_in_pyproject_fires(self) -> None:
        diff = _diff("pyproject.toml", removed=("fail_under = 93",), added=("fail_under = 90",))
        intros = scan_debt_delta(diff)
        assert [i.kind for i in intros] == ["coverage_floor_drop"]
        assert "93" in intros[0].detail
        assert "90" in intros[0].detail

    def test_lowered_cov_fail_under_in_dev_script_fires(self) -> None:
        diff = _diff(
            "dev/test-cov.sh",
            removed=("uv run pytest --cov --cov-fail-under=93",),
            added=("uv run pytest --cov --cov-fail-under=90",),
        )
        assert _kinds(diff) == ["coverage_floor_drop"]

    def test_raised_fail_under_does_not_fire(self) -> None:
        # A tightened floor is the ratchet moving the RIGHT way — never a finding.
        diff = _diff("pyproject.toml", removed=("fail_under = 90",), added=("fail_under = 93",))
        assert scan_debt_delta(diff) == []

    def test_unchanged_fail_under_does_not_fire(self) -> None:
        diff = _diff("pyproject.toml", context=("fail_under = 93",), added=("branch = true",))
        assert scan_debt_delta(diff) == []


class TestScanIsVacuousOnCleanDiff:
    def test_no_findings_on_a_clean_added_line(self) -> None:
        diff = _diff("src/teatree/m.py", added=("def clean(value: int) -> int:", "    return value + 1"))
        assert scan_debt_delta(diff) == []

    def test_empty_diff_is_empty(self) -> None:
        assert scan_debt_delta("") == []


class TestDebtWaivers:
    def _intro(self) -> DebtIntroduction:
        return DebtIntroduction(kind="noqa", path="src/teatree/m.py", line="x = f()  # noqa: F821")

    def test_load_reads_pattern_and_reason_entries(self) -> None:
        manifest = {"approved_debt": [ApprovedDebt(pattern="noqa: F821", reason="third-party stub gap")]}
        waivers = load_debt_waivers(manifest)
        assert waivers == (DebtWaiver(pattern="noqa: F821", reason="third-party stub gap"),)

    def test_load_drops_blank_reason_entries(self) -> None:
        manifest = {"approved_debt": [{"pattern": "noqa", "reason": "   "}]}
        assert load_debt_waivers(manifest) == ()

    def test_load_empty_on_missing_or_malformed_manifest(self) -> None:
        assert load_debt_waivers({}) == ()
        assert load_debt_waivers(None) == ()
        assert load_debt_waivers({"approved_debt": "not-a-list"}) == ()

    def test_waiver_covers_by_line_substring(self) -> None:
        assert waiver_covers(DebtWaiver(pattern="noqa: F821", reason="ok"), self._intro())

    def test_waiver_covers_by_kind(self) -> None:
        assert waiver_covers(DebtWaiver(pattern="noqa", reason="ok"), self._intro())

    def test_waiver_covers_by_path(self) -> None:
        assert waiver_covers(DebtWaiver(pattern="src/teatree/m.py", reason="ok"), self._intro())

    def test_blank_reason_waiver_covers_nothing(self) -> None:
        # An audited escape must SAY why — a reasonless waiver is inert.
        assert not waiver_covers(DebtWaiver(pattern="noqa", reason=""), self._intro())

    def test_non_matching_pattern_does_not_cover(self) -> None:
        assert not waiver_covers(DebtWaiver(pattern="type_ignore", reason="ok"), self._intro())

    def test_unwaived_debt_filters_covered_introductions(self) -> None:
        intros = [
            DebtIntroduction(kind="noqa", path="a.py", line="x  # noqa"),
            DebtIntroduction(kind="type_ignore", path="b.py", line="y  # type: ignore"),
        ]
        waivers = (DebtWaiver(pattern="noqa", reason="stub gap"),)
        remaining = unwaived_debt(intros, waivers)
        assert [i.kind for i in remaining] == ["type_ignore"]

    def test_unwaived_debt_returns_all_when_no_waivers(self) -> None:
        intros = [DebtIntroduction(kind="noqa", path="a.py", line="x  # noqa")]
        assert unwaived_debt(intros, ()) == intros
