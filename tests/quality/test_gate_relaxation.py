"""Anti-relaxation + tach-soundness gate engine (BLUEPRINT §17.6.1/§17.6.2, #850).

Each ``must_flag`` case is an attack-shaped diff the gate must refuse; each
``must_not_flag`` case is a legitimate diff it must let through. The pairing is
the anti-vacuity: the must-not-flag half proves the matcher is not a
block-everything, the must-flag half proves it is not a phantom gate.
"""

from teatree.quality.gate_relaxation import BLOCK, WARN, RelaxationFinding, scan_relaxation


def _diff(path: str, added: list[str], removed: list[str] | None = None) -> str:
    """A minimal unified diff adding ``added`` (and removing ``removed``) in ``path``."""
    body = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,1 +1,1 @@"]
    body.extend(f"-{line}" for line in (removed or []))
    body.extend(f"+{line}" for line in added)
    return "\n".join(body) + "\n"


def _kinds(findings: list[RelaxationFinding]) -> set[str]:
    return {f.kind for f in findings}


class TestNoqaSuppression:
    def test_bare_noqa_without_justification_blocks(self) -> None:
        findings = scan_relaxation(_diff("src/teatree/m.py", ["    x = bad()  # noqa"]))
        assert _kinds(findings) == {"noqa_without_justification"}
        assert findings[0].severity == BLOCK

    def test_coded_noqa_without_justification_blocks(self) -> None:
        findings = scan_relaxation(_diff("src/teatree/m.py", ["    x = bad()  # noqa: E501"]))
        assert _kinds(findings) == {"noqa_without_justification"}

    def test_justified_noqa_passes(self) -> None:
        findings = scan_relaxation(
            _diff("src/teatree/m.py", ["    x = bad()  # noqa: E501 — vendored URL cannot wrap"])
        )
        assert findings == []

    def test_complexity_suppression_blocks_even_when_justified(self) -> None:
        findings = scan_relaxation(_diff("src/teatree/m.py", ["def f():  # noqa: C901 — legacy, refactor later"]))
        assert _kinds(findings) == {"complexity_suppression"}

    def test_plr09xx_complexity_suppression_blocks(self) -> None:
        findings = scan_relaxation(_diff("src/teatree/m.py", ["def f(a, b, c):  # noqa: PLR0913"]))
        assert _kinds(findings) == {"complexity_suppression"}

    def test_noqa_inside_string_literal_does_not_block(self) -> None:
        # This gate's OWN detector source: a suppression marker inside a raw
        # string is not a suppression — string literals are blanked before the scan.
        findings = scan_relaxation(_diff("src/teatree/m.py", ['    pat = re.compile(r"# noqa")']))
        assert findings == []

    def test_noqa_in_pure_comment_line_does_not_block(self) -> None:
        findings = scan_relaxation(_diff("src/teatree/m.py", ["    # explains that # noqa needs a reason"]))
        assert findings == []

    def test_noqa_in_non_python_file_ignored(self) -> None:
        findings = scan_relaxation(_diff("docs/x.md", ["A new `# noqa` annotation is discouraged."]))
        assert findings == []


class TestLintCoverageConfig:
    def test_new_per_file_ignore_blocks(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ['lint.per-file-ignores."src/teatree/x.py" = [ "C901" ]']))
        assert _kinds(findings) == {"per_file_ignore_added"}

    def test_new_coverage_omit_inline_blocks(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ['omit = [ "src/teatree/new_thing/*.py" ]']))
        assert _kinds(findings) == {"coverage_omit_added"}

    def test_added_glob_to_omit_array_blocks(self) -> None:
        findings = scan_relaxation(_diff(".coveragerc", ['    "src/teatree/hard/*.py",']))
        assert _kinds(findings) == {"coverage_omit_added"}

    def test_fail_under_lowered_blocks(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ["fail_under = 80"], removed=["fail_under = 93"]))
        assert _kinds(findings) == {"coverage_floor_lowered"}
        assert "93" in findings[0].message
        assert "80" in findings[0].message

    def test_fail_under_raised_passes(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ["fail_under = 95"], removed=["fail_under = 93"]))
        assert findings == []

    def test_unrelated_pyproject_line_passes(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ['version = "1.2.3"']))
        assert findings == []

    def test_per_file_ignore_string_in_python_passes(self) -> None:
        # The gate's own source references "per-file-ignores" as prose — a .py
        # file is not a lint-config surface, so it never matches here.
        findings = scan_relaxation(_diff("src/teatree/m.py", ['    doc = "per-file-ignores is a config key"']))
        assert findings == []


class TestNoVerify:
    def test_no_verify_in_shell_blocks(self) -> None:
        findings = scan_relaxation(_diff("scripts/release.sh", ["git commit --no-verify -m ship"]))
        assert _kinds(findings) == {"no_verify_added"}

    def test_no_verify_in_ci_blocks(self) -> None:
        findings = scan_relaxation(_diff(".github/workflows/ci.yml", ["    run: git push --no-verify"]))
        assert _kinds(findings) == {"no_verify_added"}

    def test_no_verify_in_python_string_passes(self) -> None:
        # A test/gate that MENTIONS the flag in Python is not a committed bypass.
        findings = scan_relaxation(_diff("src/teatree/m.py", ['    BLOCKED = "--no-verify"']))
        assert findings == []


class TestTachSoundness:
    def test_new_empty_interfaces_blocks(self) -> None:
        findings = scan_relaxation(_diff("tach.toml", ["interfaces = []"]))
        assert _kinds(findings) == {"empty_interfaces_added"}

    def test_ignore_type_checking_without_comment_blocks(self) -> None:
        findings = scan_relaxation(_diff("tach.toml", ["ignore_type_checking_imports = true"]))
        assert _kinds(findings) == {"type_check_ignore_without_comment"}

    def test_ignore_type_checking_with_comment_passes(self) -> None:
        findings = scan_relaxation(
            _diff("tach.toml", ["# unavoidable: circular typing-only import", "ignore_type_checking_imports = true"])
        )
        assert findings == []

    def test_populated_interfaces_passes(self) -> None:
        findings = scan_relaxation(_diff("tach.toml", ['interfaces = ["public_fn"]']))
        assert findings == []

    def test_interfaces_string_in_non_tach_file_ignored(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ["interfaces = []"]))
        assert "empty_interfaces_added" not in _kinds(findings)


class TestTestVacuityWarn:
    def test_assertionless_added_test_warns_only(self) -> None:
        findings = scan_relaxation(_diff("tests/test_x.py", ["def test_it():", "    x = compute()"]))
        assert _kinds(findings) == {"possible_test_vacuity"}
        assert findings[0].severity == WARN

    def test_added_test_with_assert_passes(self) -> None:
        findings = scan_relaxation(_diff("tests/test_x.py", ["def test_it():", "    assert compute() == 1"]))
        assert findings == []

    def test_added_test_with_pytest_raises_passes(self) -> None:
        findings = scan_relaxation(
            _diff("tests/test_x.py", ["def test_it():", "    with pytest.raises(ValueError):", "        compute()"])
        )
        assert findings == []


class TestVacuousOnEmpty:
    def test_empty_diff_yields_nothing(self) -> None:
        assert scan_relaxation("") == []

    def test_pure_context_diff_yields_nothing(self) -> None:
        assert scan_relaxation(_diff("src/teatree/m.py", [], removed=[])) == []
