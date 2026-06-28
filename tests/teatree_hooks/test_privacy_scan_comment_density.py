"""Tests for the advisory ``code_comment_density`` diff pass.

This is the commit-side half of the comments-as-code rule (the dispatch-side
half lives in the sub-agent prompt preambles). The detector is content-aware:
beyond a conservative comment:code ratio and a consecutive comment-only run, it
flags a comment whose words merely restate the next code line and a docstring
opening that merely echoes the signature, while leaving a genuine
non-obvious-why comment and a justified multi-line docstring allowed.

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


class TestRestatingCommentDetection:
    """Content-aware: a comment that merely restates the next code line is flagged.

    The pure line-count density pass misses a SINGLE restating comment below the
    consecutive-run threshold (the verbose-inline-comment style trimmed in real
    review). A comment whose content-words are a subset of the following code
    line's identifier tokens carries no non-obvious why — it restates the code —
    and is flagged regardless of run length.
    """

    def test_single_restating_comment_above_code_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def to_eur(cents):",
            "    # divide the cents by one hundred",
            "    return cents / 100",
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_inline_comment_restating_the_update_call_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def backfill(model):",
            "    # update the model rows with the metadata",
            "    model.objects.update(**metadata)",
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_non_obvious_why_comment_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def render(value):",
            "    # SandboxedEnvironment blocks attribute-access SSTI",
            "    env = SandboxedEnvironment()",
            "    return env.from_string(value).render()",
        )
        assert report_diff(diff) == []

    def test_comment_with_rationale_words_not_in_code_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def backfill(model):",
            "    # back-fill pre-existing rows created before this shipped",
            "    model.objects.update(**metadata)",
        )
        assert report_diff(diff) == []

    def test_restating_comment_at_end_of_hunk_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "    total = a + b",
            "    # return the total",
            "    return total",
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_restating_comment_resolves_against_an_unchanged_context_line(self) -> None:
        diff = (
            "diff --git a/src/teatree/x.py b/src/teatree/x.py\n"
            "--- a/src/teatree/x.py\n"
            "+++ b/src/teatree/x.py\n"
            "@@ -3,2 +3,3 @@\n"
            "     total = a + b\n"
            "+    # return the total\n"
            "     return total\n"
        )
        assert _paths(diff) == ["src/teatree/x.py"]


class TestSignatureEchoDocstring:
    """A docstring that merely echoes the signature is flagged; a real one is not.

    A genuine multi-line docstring carrying a non-obvious why stays ALLOWED — the
    target is the vacuous docstring that repeats the function/class name and adds
    nothing (the verbose-docstring style trimmed in real review).
    """

    def test_docstring_echoing_the_function_name_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def add_feature_flag(apps, schema_editor):",
            '    """Add the feature flag."""',
            "    apps.get_model('shared', 'FeatureFlag').objects.create()",
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_multiline_docstring_restating_the_code_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def remove_feature_flag(apps, schema_editor):",
            '    """Remove the feature flag.',
            "",
            "    Removes the feature flag row from the database.",
            "    Deletes the feature flag for every customer.",
            '    """',
            "    Flag.objects.filter(name=NAME).delete()",
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_genuine_docstring_with_non_obvious_why_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def add_feature_flag(apps, schema_editor):",
            '    """Seed the platform-messages flag, OFF by default.',
            "",
            "    The endpoint reports zero messages while OFF so the feature",
            "    stays dark per tenant until each environment opts in.",
            '    """',
            "    Flag.objects.get_or_create(name=NAME, defaults={'value': False})",
        )
        assert report_diff(diff) == []

    def test_class_docstring_echoing_the_class_name_is_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "class PaymentProcessor:",
            '    """Process the payment."""',
            "    def run(self):",
            "        return self.gateway.charge()",
        )
        assert _paths(diff) == ["src/teatree/x.py"]

    def test_module_docstring_is_not_treated_as_signature_echo(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            '"""Money formatting helpers."""',
            "",
            "def to_eur(cents: int) -> str:",
            "    euros, rest = divmod(cents, 100)",
            "    return f'{euros},{rest:02d} EUR'",
        )
        assert report_diff(diff) == []

    def test_one_word_docstring_is_too_short_to_count_as_echo(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def add_feature_flag(apps, schema_editor):",
            '    """Idempotent."""',
            "    Flag.objects.get_or_create(name=NAME, defaults={'value': False})",
        )
        assert report_diff(diff) == []

    def test_symbol_only_comment_carries_no_content_words(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "def handle(value):",
            "    counter = value + 1",
            "    # ----------------------------",
            "    return counter",
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
