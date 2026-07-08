"""Anti-relaxation + tach-soundness gate engine (BLUEPRINT §17.6.1/§17.6.2, #850).

Each ``must_flag`` case is an attack-shaped diff the gate must refuse; each
``must_not_flag`` case is a legitimate diff it must let through. The pairing is
the anti-vacuity: the must-not-flag half proves the matcher is not a
block-everything, the must-flag half proves it is not a phantom gate.
"""

from teatree.quality.gate_relaxation import BLOCK, WARN, RelaxationFinding, parse_diff, scan_relaxation


def _diff(path: str, added: list[str], removed: list[str] | None = None) -> str:
    """A minimal unified diff adding ``added`` (and removing ``removed``) in ``path``."""
    body = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,1 +1,1 @@"]
    body.extend(f"-{line}" for line in (removed or []))
    body.extend(f"+{line}" for line in added)
    return "\n".join(body) + "\n"


def _raw_diff(path: str, hunk_lines: list[str]) -> str:
    """A raw unified diff for *path* whose hunk body is *hunk_lines* (each carries its own +/-/space marker)."""
    header = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1,4 +1,5 @@"]
    return "\n".join(header + hunk_lines) + "\n"


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

    def test_preexisting_complexity_code_with_sibling_stripped_does_not_block(self) -> None:
        # RUF100 strips redundant sibling codes (PLR6301/ARG002 covered by a
        # per-file-ignore), leaving a lone PLR0913 that was ALREADY on the same
        # line at base. The noqa line "changed", but no complexity code is new,
        # so the diff-aware matcher must not flag it.
        findings = scan_relaxation(
            _diff(
                "src/teatree/core/overlay.py",
                ["    def db_import(  # noqa: PLR0913 — overlay extension-point contract; documented hook inputs."],
                removed=["    def db_import(  # noqa: PLR0913, PLR6301, ARG002 — overlay extension-point contract."],
            )
        )
        assert findings == []

    def test_complexity_code_newly_added_to_existing_line_still_blocks(self) -> None:
        # A complexity code (C901) added to a line that already suppressed a
        # DIFFERENT complexity code (PLR0913) is a genuinely new relaxation.
        findings = scan_relaxation(
            _diff(
                "src/teatree/m.py",
                ["def f(a, b, c):  # noqa: PLR0913, C901"],
                removed=["def f(a, b, c):  # noqa: PLR0913"],
            )
        )
        assert _kinds(findings) == {"complexity_suppression"}

    def test_preexisting_unjustified_code_with_sibling_stripped_does_not_block(self) -> None:
        # Same sibling-strip class on an unjustified non-complexity noqa: E501 was
        # already suppressed on this line at base; dropping the redundant sibling
        # is not a new relaxation.
        findings = scan_relaxation(
            _diff(
                "src/teatree/m.py",
                ["    x = bad()  # noqa: E501"],
                removed=["    x = bad()  # noqa: E501, PLR6301"],
            )
        )
        assert findings == []

    def test_new_unjustified_code_added_to_existing_line_still_blocks(self) -> None:
        # E501 added (unjustified) to a line that previously only suppressed F401
        # is a genuinely new suppression — the pre-existing F401 does not excuse it.
        findings = scan_relaxation(
            _diff(
                "src/teatree/m.py",
                ["    x = bad()  # noqa: F401, E501"],
                removed=["    x = bad()  # noqa: F401"],
            )
        )
        assert _kinds(findings) == {"noqa_without_justification"}

    def test_bare_colon_noqa_without_codes_or_reason_blocks(self) -> None:
        # A `# noqa:` with no codes and no justification is an unjustified blanket
        # suppression — the code-list parse finds no token and it still blocks.
        findings = scan_relaxation(_diff("src/teatree/m.py", ["    x = bad()  # noqa:"]))
        assert _kinds(findings) == {"noqa_without_justification"}

    def test_new_suppression_over_plain_removed_line_still_blocks(self) -> None:
        # The removed base line carried no noqa, so the added suppression is new
        # even though the same code stem changed in this hunk.
        findings = scan_relaxation(
            _diff(
                "src/teatree/m.py",
                ["def f(a, b, c):  # noqa: PLR0913"],
                removed=["def f(a):"],
            )
        )
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

    def test_regular_trailing_comment_does_not_block(self) -> None:
        # Code before a non-`noqa` trailing comment: real code precedes the `#`
        # but the marker isn't a suppression, so the noqa matcher skips the line.
        findings = scan_relaxation(_diff("src/teatree/m.py", ["    x = compute()  # keep the intermediate value"]))
        assert findings == []


class TestLintCoverageConfig:
    def test_new_per_file_ignore_blocks(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ['lint.per-file-ignores."src/teatree/x.py" = [ "C901" ]']))
        assert _kinds(findings) == {"per_file_ignore_added"}

    def test_new_coverage_omit_inline_blocks(self) -> None:
        findings = scan_relaxation(_diff("pyproject.toml", ['omit = [ "src/teatree/new_thing/*.py" ]']))
        assert _kinds(findings) == {"coverage_omit_added"}

    def test_new_omit_array_with_globs_blocks(self) -> None:
        # An INI `.coveragerc` omit list added wholesale — the `omit =` opening plus
        # its indented glob entries — is a real coverage relaxation.
        findings = scan_relaxation(_diff(".coveragerc", ["omit =", "    src/teatree/hard/*.py"]))
        assert _kinds(findings) == {"coverage_omit_added"}

    def test_glob_added_to_existing_omit_array_flagged(self) -> None:
        # A glob added inside a coverage `omit` array whose opening is a CONTEXT line
        # IS a coverage omit — the enclosing-array is tracked from the diff context.
        diff = _raw_diff(
            "pyproject.toml",
            [
                " [tool.coverage.run]",
                " omit = [",
                '     "src/teatree/legacy/*.py",',
                '+    "src/teatree/hard/*.py",',
                " ]",
            ],
        )
        assert _kinds(scan_relaxation(diff)) == {"coverage_omit_added"}

    def test_ruff_exclude_glob_not_flagged_as_coverage_omit(self) -> None:
        # A `*`-glob added inside a ruff `exclude` array is NOT a coverage omit — the
        # old context-free matcher false-positived on any quoted glob in a lint/cov file.
        diff = _raw_diff(
            "pyproject.toml",
            [
                " [tool.ruff]",
                " exclude = [",
                '     "generated/*",',
                '+    "build/*",',
                " ]",
            ],
        )
        assert "coverage_omit_added" not in _kinds(scan_relaxation(diff))

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

    def test_test_file_diff_without_test_def_yields_nothing(self) -> None:
        # A test-file diff that adds no `def test_` (a helper edit) is not a
        # vacuity candidate — the heuristic only fires on an added test function.
        findings = scan_relaxation(_diff("tests/test_x.py", ["    helper = build_fixture()"]))
        assert findings == []


class TestVacuousOnEmpty:
    def test_empty_diff_yields_nothing(self) -> None:
        assert scan_relaxation("") == []

    def test_pure_context_diff_yields_nothing(self) -> None:
        assert scan_relaxation(_diff("src/teatree/m.py", [], removed=[])) == []


class TestParseDiff:
    def test_body_line_before_any_file_header_is_dropped(self) -> None:
        # A `+`/`-` body line preceding the first `+++ b/` header has no owning
        # file — it must be dropped, not attributed to the next file's block.
        diff = "+orphan body line with no preceding file header\n" + _diff("src/teatree/m.py", ["    x = 1"])
        result = parse_diff(diff)
        assert [fd.path for fd in result] == ["src/teatree/m.py"]
        assert result[0].added == ["    x = 1"]

    def test_removed_line_followed_by_context_line(self) -> None:
        # A context (unchanged) line — leading space, neither `+` nor `-` — is
        # collected into neither the added nor the removed set.
        diff = (
            "diff --git a/src/teatree/m.py b/src/teatree/m.py\n"
            "--- a/src/teatree/m.py\n"
            "+++ b/src/teatree/m.py\n"
            "@@ -1,2 +1,1 @@\n"
            "-old = 1\n"
            " kept = 2\n"
        )
        result = parse_diff(diff)
        assert result[0].removed == ["old = 1"]
        assert result[0].added == []

    def test_deleted_file_removed_lines_do_not_bleed_into_previous_file(self) -> None:
        # A `+++ /dev/null` header (a deleted file) resets the accumulator, so the
        # deleted file's `-` lines are NOT appended to the previous file's removed set.
        diff = (
            "diff --git a/keep.py b/keep.py\n"
            "--- a/keep.py\n"
            "+++ b/keep.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old_keep = 1\n"
            "+new_keep = 1\n"
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-gone_1 = 1\n"
            "-gone_2 = 2\n"
        )
        result = parse_diff(diff)
        keep = next(fd for fd in result if fd.path == "keep.py")
        assert keep.removed == ["old_keep = 1"]  # NOT polluted by gone.py's removed lines
        assert keep.added == ["new_keep = 1"]
        assert all(fd.path != "gone.py" for fd in result)  # the deleted file yields no _FileDiff
