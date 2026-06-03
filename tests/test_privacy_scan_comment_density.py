"""Tests for the advisory ``code_comment_density`` density pass.

This is the commit-side half of the near-zero-comments rule (the dispatch-side
half lives in the sub-agent prompt preambles). The detector is content-blind:
it flags a file whose added diff lines are either comment-heavy (ratio above a
conservative threshold) or carry a block of consecutive comment-only lines past
the warn threshold in non-test code.

The check is **advisory** and deliberately NOT one of ``privacy_scan.py``'s
blocking diff detectors — a comment-dense diff is a code-quality nudge, not a
privacy leak, so it must never block a push. This module pins both the
detection rules (via :func:`report_diff`) and that contract: density is absent
from ``privacy_scan._DIFF_DETECTORS`` while the remaining detector still runs
fail-open.

The detector is diff-aware: it tracks the current file from the unified-diff
``+++ b/<path>`` headers, scans **added lines only** (``^+``), classifies each
as comment-only / code / docstring on the file language's comment syntax
(Python ``#``, JS/TS ``//`` and ``/* */``), and exempts markdown/docs
(``*.md``, ``docs/``), ``tests/``, docstring bodies, and a small
security-rationale allowlist (a comment beginning with the agreed marker).
"""

from unittest.mock import Mock, patch

from scripts import privacy_scan
from teatree.hooks import privacy_diff_comment_density
from teatree.hooks.privacy_diff_comment_density import report_diff


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


def _diff_at(path: str, start: int, *added_lines: str) -> str:
    body = "".join(f"+{line}\n" for line in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 0000000..1111111 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -{start},0 +{start},{len(added_lines)} @@\n"
        f"{body}"
    )


def _paths(text: str) -> list[str]:
    return [f.path for f in report_diff(text)]


class TestDetectorUnit:
    """Direct coverage of the pure diff-parsing detector via report_diff."""

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
        assert _paths(diff) == ["src/teatree/x.py"]

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
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_scattered_comment_heavy_diff_is_flagged_by_ratio(self) -> None:
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
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_sparse_code_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    counter = value + 1",
            "    cache[key] = counter",
            "    return counter",
        )
        assert report_diff(diff) == []

    def test_one_explanatory_comment_among_code_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # SandboxedEnvironment blocks attribute-access SSTI",
            "    env = SandboxedEnvironment()",
            "    template = env.from_string(value)",
            "    return template.render()",
        )
        assert report_diff(diff) == []

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
        assert report_diff(diff) == []

    def test_tests_path_is_exempt(self) -> None:
        diff = _diff(
            "tests/test_x.py",
            "def test_handle():",
            "    # increment the counter by one",
            "    # then store it in the cache",
            "    # so later reads are fast",
            "    assert handle(1) == 2",
        )
        assert report_diff(diff) == []

    def test_markdown_is_exempt(self) -> None:
        diff = _diff(
            "docs/notes.md",
            "# heading",
            "# another heading",
            "# yet another heading",
            "some prose here",
        )
        assert report_diff(diff) == []

    def test_security_rationale_comments_are_exempt(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # security: untrusted input must be validated before use",
            "    # security: the sandbox denies attribute access to block SSTI",
            "    # security: rendering happens only after the allowlist check",
            "    return render(validate(value))",
        )
        assert report_diff(diff) == []

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
        assert report_diff(diff) == []

    def test_unknown_suffix_has_no_comment_syntax(self) -> None:
        diff = _diff(
            "src/templates/page.html",
            "<!-- a -->",
            "<!-- b -->",
            "<!-- c -->",
            "<div>x</div>",
        )
        assert report_diff(diff) == []

    def test_allow_marker_lines_are_skipped(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "x = compute()",
            "    # narrate one  # privacy-scan:allow",
            "    # narrate two  # privacy-scan:allow",
            "    # narrate three  # privacy-scan:allow",
            "y = x + 1",
            "z = y + 1",
        )
        assert report_diff(diff) == []

    def test_blank_added_lines_are_not_counted_as_code(self) -> None:
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
        assert _paths(diff) == ["src/teatree/x.py"]

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
        assert _paths(diff) == ["src/app/widget.ts"]

    def test_leading_license_header_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "# Copyright 2026 Example",
            "# SPDX-License-Identifier: Apache-2.0",
            "# Licensed under the Apache License, Version 2.0",
            "",
            "import os",
            "value = os.getcwd()",
        )
        assert report_diff(diff) == []

    def test_four_line_leading_header_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "#!/usr/bin/env python",
            "# -*- coding: utf-8 -*-",
            "# Copyright 2026 Example",
            "# SPDX-License-Identifier: MIT",
            "",
            "import sys",
            "print(sys.argv)",
        )
        assert report_diff(diff) == []

    def test_license_marker_header_not_at_line_one_is_not_flagged(self) -> None:
        diff = _diff_at(
            "src/teatree/x.py",
            40,
            "# Copyright 2026 Example",
            "# Licensed under the Apache License, Version 2.0",
            "# SPDX-License-Identifier: Apache-2.0",
            "value = compute()",
        )
        assert report_diff(diff) == []

    def test_mid_code_narration_after_leading_header_still_flags(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "# Copyright 2026 Example",
            "# SPDX-License-Identifier: Apache-2.0",
            "",
            "def handle(value):",
            "    # increment the counter by one",
            "    # then store it in the cache",
            "    # so later reads are fast",
            "    counter += 1",
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_context_lines_advance_target_line_so_mid_file_comments_still_flag(self) -> None:
        diff = (
            "diff --git a/src/teatree/x.py b/src/teatree/x.py\n"
            "--- a/src/teatree/x.py\n"
            "+++ b/src/teatree/x.py\n"
            "@@ -1,3 +1,6 @@\n"
            " import os\n"
            " import sys\n"
            " value = os.getcwd()\n"
            "+    # narrate the first step\n"
            "+    # narrate the second step\n"
            "+    # narrate the third step\n"
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_blank_context_line_keeps_top_of_file_header_exempt(self) -> None:
        diff = (
            "diff --git a/src/teatree/x.py b/src/teatree/x.py\n"
            "--- a/src/teatree/x.py\n"
            "+++ b/src/teatree/x.py\n"
            "@@ -1,1 +1,5 @@\n"
            " \n"
            "+# Copyright 2026 Example\n"
            "+# SPDX-License-Identifier: Apache-2.0\n"
            "+# Licensed under the Apache License, Version 2.0\n"
            "+import os\n"
        )
        assert report_diff(diff) == []


class TestNotAPrivacyGateDetector:
    """Density is advisory, so it must NOT block the public-repo privacy scan."""

    def test_density_is_absent_from_blocking_diff_detectors(self) -> None:
        names = [getattr(d, "__module__", "") for d in privacy_scan._DIFF_DETECTORS]
        assert not any("comment_density" in n for n in names)

    def test_comment_dense_diff_yields_no_privacy_findings(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    # increment the counter by one",
            "    # then store it in the cache",
            "    # so later reads are fast",
            "    counter += 1",
            "    cache[key] = counter",
        )
        assert privacy_scan._run_diff_detectors(diff) == []


class TestDetectorFailsOpen:
    """A crash inside a diff detector must be cannot-evaluate, never a deny."""

    def test_detector_exception_does_not_block_the_scan(self) -> None:
        boom = Mock(side_effect=RuntimeError("detector blew up"))
        diff = _diff("src/teatree/x.py", "x = 1")
        with patch.object(privacy_scan, "_DIFF_DETECTORS", (boom,)):
            findings = privacy_scan._run_diff_detectors(diff)
        assert findings == []


def test_module_no_longer_exports_scan_diff() -> None:
    assert not hasattr(privacy_diff_comment_density, "scan_diff")
