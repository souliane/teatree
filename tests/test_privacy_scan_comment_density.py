"""Tests for the ``code_comment_density`` detector in ``privacy_scan.py``.

This is the commit-side half of the near-zero-comments rule (the
dispatch-side half lives in the sub-agent prompt preambles). The existing
``code_comment_self_reference`` detector is content-aware — it matches
bookkeeping tokens (MR/ticket/workstream refs). It therefore misses plain
WHAT-narration comments that carry no tracker reference at all. This
detector is content-blind: it flags a file whose added diff lines are
either comment-heavy (ratio above a conservative threshold) or carry a
block of 3+ consecutive comment-only lines in non-test code.

The detector is diff-aware: it tracks the current file from the
unified-diff ``+++ b/<path>`` headers, scans **added lines only** (``^+``),
classifies each as comment-only / code / docstring on the file language's
comment syntax (Python ``#``, JS/TS ``//`` and ``/* */``), and exempts
markdown/docs (``*.md``, ``docs/``), ``tests/``, docstring bodies, and a
small security-rationale allowlist (a comment beginning with the agreed
marker).

A crash inside the detector must FAIL OPEN — the scanner skips it rather
than denying the push (the gate-overdeny rule).
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import privacy_scan
from scripts.privacy_scan import PRIVACY_FINDINGS_EXIT_CODE
from teatree.hooks import privacy_diff_comment_density

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py"

_CATEGORY = "code_comment_density"


def _run(stdin: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-", *extra],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _diff(path: str, *added_lines: str) -> str:
    body = "".join(f"+{line}\n" for line in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 0000000..1111111 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added_lines)} @@\n"
        f"{body}"
    )


class TestDetectorUnit:
    """Direct coverage of the pure diff-parsing detector."""

    def test_dense_what_narration_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # increment the counter by one",
            "    # then store it in the cache",
            "    # so later reads are fast",
            "    counter += 1",
            "    cache[key] = counter",
        )
        findings = privacy_diff_comment_density.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_three_consecutive_comment_block_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "result = compute(a, b, c, d)",
            "result = compute(e, f, g, h)",
            "result = compute(i, j, k, l)",
            "result = compute(m, n, o, p)",
            "result = compute(q, r, s, t)",
            "result = compute(u, v, w, x)",
            "result = compute(y, z, a, b)",
            "    # first the inputs are normalised",
            "    # then the totals are summed",
            "    # finally the result is rounded",
            "final = round(result)",
        )
        findings = privacy_diff_comment_density.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_scattered_comment_heavy_diff_is_flagged_by_ratio(self) -> None:
        # No run of 3+ consecutive comments, so this exercises the ratio
        # rule independently of the consecutive-block rule.
        diff = _diff(
            "src/teatree/x.py",
            "a = 1",
            "    # narrate the first step",
            "b = 2",
            "    # narrate the second step",
            "c = 3",
            "    # narrate the third step",
            "d = 4",
        )
        findings = privacy_diff_comment_density.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_sparse_code_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    counter = value + 1",
            "    cache[key] = counter",
            "    return counter",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_one_explanatory_comment_among_code_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # SandboxedEnvironment blocks attribute-access SSTI",
            "    env = SandboxedEnvironment()",
            "    template = env.from_string(value)",
            "    return template.render()",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_docstring_body_is_not_counted_as_comment(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            '    """Normalise and persist the value.',
            "",
            "    The value is normalised then written through to the cache",
            "    so subsequent reads stay fast under load.",
            '    """',
            "    counter = value + 1",
            "    cache[key] = counter",
            "    return counter",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_tests_path_is_exempt(self) -> None:
        diff = _diff(
            "tests/test_x.py",
            "def test_handle():",
            "    # increment the counter by one",
            "    # then store it in the cache",
            "    # so later reads are fast",
            "    assert handle(1) == 2",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_markdown_is_exempt(self) -> None:
        diff = _diff(
            "docs/notes.md",
            "# heading",
            "# another heading",
            "# yet another heading",
            "some prose here",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_security_rationale_comments_are_exempt(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # security: untrusted input must be validated before use",
            "    # security: the sandbox denies attribute access to block SSTI",
            "    # security: rendering happens only after the allowlist check",
            "    return render(validate(value))",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_removed_comment_lines_are_not_scanned(self) -> None:
        diff = (
            "diff --git a/src/teatree/x.py b/src/teatree/x.py\n"
            "--- a/src/teatree/x.py\n"
            "+++ b/src/teatree/x.py\n"
            "@@ -1,4 +1,1 @@\n"
            "-    # increment the counter by one\n"
            "-    # then store it in the cache\n"
            "-    # so later reads are fast\n"
            "+    counter += 1\n"
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_unknown_suffix_has_no_comment_syntax(self) -> None:
        diff = _diff(
            "src/templates/page.html",
            "<!-- a -->",
            "<!-- b -->",
            "<!-- c -->",
            "<div>x</div>",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_allow_marker_lines_are_skipped(self) -> None:
        # A comment line carrying the allow-marker is exempt, so the run of
        # comment lines never reaches the consecutive-block threshold.
        diff = _diff(
            "src/teatree/x.py",
            "x = compute()",
            "    # narrate one  # privacy-scan:allow",
            "    # narrate two  # privacy-scan:allow",
            "    # narrate three  # privacy-scan:allow",
            "y = x + 1",
            "z = y + 1",
        )
        assert privacy_diff_comment_density.scan_diff(diff) == []

    def test_blank_added_lines_are_not_counted_as_code(self) -> None:
        # Blank added lines are neither comment nor code, so they neither
        # inflate the code denominator nor break a comment run.
        diff = _diff(
            "src/teatree/x.py",
            "a = 1",
            "",
            "    # narrate one",
            "    # narrate two",
            "    # narrate three",
            "",
            "b = 2",
        )
        findings = privacy_diff_comment_density.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_ts_block_comment_run_is_flagged(self) -> None:
        diff = _diff(
            "src/app/widget.ts",
            "const total = a + b + c + d;",
            "const ratio = total / count;",
            "const scaled = ratio * factor;",
            "const clamped = Math.min(scaled, max);",
            "const result = Math.max(clamped, min);",
            "  // normalise the raw inputs first",
            "  // then apply the scaling factor",
            "  // and clamp into the valid range",
            "return result;",
        )
        findings = privacy_diff_comment_density.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)


class TestDetectorFailsOpen:
    """A crash inside a diff detector must be cannot-evaluate, never a deny."""

    def test_detector_exception_does_not_block_the_scan(self) -> None:
        boom = Mock(side_effect=RuntimeError("detector blew up"))
        diff = _diff("src/teatree/x.py", "x = 1")
        with patch.object(privacy_scan, "_DIFF_DETECTORS", (boom,)):
            findings = privacy_scan._run_diff_detectors(diff)
        assert findings == []


class TestScriptEndToEnd:
    """The script blocks a comment-dense diff and passes sparse code."""

    def test_dense_comment_diff_exits_findings_code(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # increment the counter by one",
            "    # then store it in the cache",
            "    # so later reads are fast",
            "    counter += 1",
            "    cache[key] = counter",
        )
        result = _run(diff)
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert _CATEGORY in result.stdout

    def test_sparse_code_exits_zero(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    counter = value + 1",
            "    cache[key] = counter",
            "    return counter",
        )
        result = _run(diff)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_json_output_carries_the_new_category(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # increment the counter by one",
            "    # then store it in the cache",
            "    # so later reads are fast",
            "    counter += 1",
            "    cache[key] = counter",
        )
        proc = _run(diff, "--json")
        parsed = json.loads(proc.stdout)
        assert any(f["category"] == _CATEGORY for f in parsed)
