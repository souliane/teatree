"""Tests for the ``code_comment_self_reference`` detector in ``privacy_scan.py``.

The diff privacy-scanner (``t3 tool privacy-scan``, wired into the pre-push
gate ``refuse-public-push-with-leak.sh``) already scans the pushed diff for
emails / keys / banned terms. This detector extends it to a recurring leak
class the prose rule keeps missing: **bookkeeping self-references left in
code comments** — a "consolidated into <bang-MR-ref>" note, a
workstream-number tag, a tracker-id reference, a "per pentest" aside.

The detector is diff-aware: it tracks the current file from the unified-diff
``+++ b/<path>`` headers, scans **added lines only** (``^+``), matches only
inside the file language's comment syntax (Python ``#``, JS/TS ``//`` and
``/* */``), and exempts markdown/docs (``*.md``, ``docs/``, ``CHANGELOG*``)
which legitimately cite MRs/tickets. Legit security-why comments (a threat
explained, no bookkeeping ref) must NOT be flagged.

Tests invoke the script the same way ``run_script`` / the pre-push gate do
(stdin pipe, captured streams), so the entrypoint is exercised, not mocked.
"""

import json
import subprocess
import sys
from pathlib import Path

from scripts.privacy_scan import PRIVACY_FINDINGS_EXIT_CODE
from teatree.hooks import privacy_diff_comments

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "privacy_scan.py"

_CATEGORY = "code_comment_self_reference"


def _run(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _diff(path: str, *added_lines: str) -> str:
    """Build a minimal unified diff adding ``added_lines`` to ``path``."""
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
    """Direct unit coverage of the pure diff-parsing detector."""

    def test_python_mr_ref_comment_is_flagged(self) -> None:
        diff = _diff("src/teatree/x.py", "    # Consolidated into !7511")  # privacy-scan:allow
        findings = privacy_diff_comments.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_ts_workstream_number_comment_is_flagged(self) -> None:
        diff = _diff("src/app/widget.ts", "  // W20 sandbox fix")  # privacy-scan:allow
        findings = privacy_diff_comments.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_block_comment_mr_ref_is_flagged(self) -> None:
        diff = _diff("src/app/widget.ts", "  /* superseded by !6264 */")  # privacy-scan:allow
        findings = privacy_diff_comments.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_legit_security_comment_is_not_flagged(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "    # SandboxedEnvironment blocks attribute-access SSTI",
        )
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_markdown_self_reference_is_exempt(self) -> None:
        diff = _diff("docs/BLUEPRINT.md", "see !7511 for the consolidation")  # privacy-scan:allow
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_top_level_md_is_exempt(self) -> None:
        diff = _diff("AGENTS.md", "<!-- consolidated into !7511 -->")  # privacy-scan:allow
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_changelog_is_exempt(self) -> None:
        diff = _diff("CHANGELOG.rst", "# PROJ-2141 fixed")  # privacy-scan:allow
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_identifier_named_mr_is_not_flagged(self) -> None:
        # ``mr_id`` is a code identifier, not a comment self-reference.
        diff = _diff("src/teatree/x.py", "result = process(mr_id)  # not a comment ref")
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_removed_line_is_not_scanned(self) -> None:
        # A removed line (``-``) carrying a ref must not be flagged — only
        # ADDED lines land in the new history.
        diff = (
            "diff --git a/src/teatree/x.py b/src/teatree/x.py\n"
            "--- a/src/teatree/x.py\n"
            "+++ b/src/teatree/x.py\n"
            "@@ -1,1 +0,0 @@\n"
            "-    # Consolidated into !7511\n"  # privacy-scan:allow
        )
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_ticket_id_in_python_comment_is_flagged(self) -> None:
        diff = _diff("src/teatree/x.py", "    # PROJ-2141 needs this")  # privacy-scan:allow
        findings = privacy_diff_comments.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_crypto_standard_token_is_not_flagged(self) -> None:
        # ``SHA-256`` / ``RFC-3339`` share the tracker-key shape but are
        # standard identifiers, not ticket bookkeeping.
        for token in ("SHA-256", "RFC-3339", "ISO-8601", "AES-256"):
            diff = _diff("src/teatree/x.py", f"    # hash via {token} here")
            assert privacy_diff_comments.scan_diff(diff) == [], token

    def test_tracker_key_and_crypto_token_on_same_comment_is_flagged(self) -> None:
        # A real tracker ref alongside a standard token is still caught.
        diff = _diff("src/teatree/x.py", "    # SHA-256 digest tracked in PROJ-77")  # privacy-scan:allow
        findings = privacy_diff_comments.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_process_narration_pentest_is_flagged(self) -> None:
        diff = _diff("src/teatree/x.py", "    # widen scope per pentest")  # privacy-scan:allow
        findings = privacy_diff_comments.scan_diff(diff)
        assert any(c == _CATEGORY for _, c, _ in findings)

    def test_non_diff_plain_text_is_ignored(self) -> None:
        # Plain text (no diff structure) yields no comment findings — the
        # detector keys on ``+++ b/<path>`` + ``+`` markers.
        assert privacy_diff_comments.scan_diff("# Consolidated into !7511\n") == []  # privacy-scan:allow

    def test_added_ref_outside_comment_is_not_flagged(self) -> None:
        # An MR ref in a string literal that is legitimately data, not a
        # comment, must not be flagged.
        diff = _diff("src/teatree/x.py", '    url = "https://x/-/merge_requests/7511"')
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_nested_docs_dir_is_exempt(self) -> None:
        # A ``docs/`` segment anywhere in the path (not only at the root)
        # exempts the file — even a source-suffix file living under docs/.
        diff = _diff("packages/docs/snippet.py", "    # Consolidated into !7511")  # privacy-scan:allow
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_root_docs_dir_is_exempt(self) -> None:
        # A top-level ``docs/`` prefix exempts a source-suffix file too.
        diff = _diff("docs/snippet.py", "    # PROJ-2141 fixed")  # privacy-scan:allow
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_unknown_suffix_has_no_comment_syntax(self) -> None:
        # A file with no recognised comment syntax yields no findings even
        # when the added line carries a ref-shaped substring.
        diff = _diff("src/templates/page.html", "<div>!7511</div>")  # privacy-scan:allow
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_allow_marker_on_added_comment_is_exempt(self) -> None:
        diff = _diff("src/teatree/x.py", "    # Consolidated into !7511  # privacy-scan:allow")
        assert privacy_diff_comments.scan_diff(diff) == []

    def test_line_number_is_reported(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "    clean = True",
            "    # Consolidated into !7511",  # privacy-scan:allow
        )
        findings = privacy_diff_comments.scan_diff(diff)
        assert findings
        # The finding's reported line is the line within the scanned text.
        assert all(isinstance(lineno, int) and lineno > 0 for lineno, _, _ in findings)


class TestScriptEndToEnd:
    """The script blocks a diff with an MR-ref comment on an added line."""

    def test_python_mr_comment_exits_findings_code(self) -> None:
        diff = _diff("src/teatree/x.py", "    # Consolidated into !7511")  # privacy-scan:allow
        result = _run(diff)
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert _CATEGORY in result.stdout

    def test_ts_workstream_comment_exits_findings_code(self) -> None:
        diff = _diff("src/app/widget.ts", "  // W20 sandbox fix")  # privacy-scan:allow
        result = _run(diff)
        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert _CATEGORY in result.stdout

    def test_legit_security_comment_exits_zero(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "    # SandboxedEnvironment blocks attribute-access SSTI",
        )
        result = _run(diff)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_markdown_self_reference_exits_zero(self) -> None:
        diff = _diff("docs/BLUEPRINT.md", "see !7511 for the consolidation")  # privacy-scan:allow
        result = _run(diff)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_identifier_mr_does_not_false_positive(self) -> None:
        diff = _diff("src/teatree/x.py", "result = process(mr_id)  # not a comment ref")
        result = _run(diff)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_json_output_carries_the_new_category(self) -> None:
        diff = _diff("src/teatree/x.py", "    # PROJ-2141 needs this")  # privacy-scan:allow
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "-", "--json"],
            input=diff,
            capture_output=True,
            text=True,
            check=False,
        )
        parsed = json.loads(proc.stdout)
        assert any(f["category"] == _CATEGORY for f in parsed)

    def test_allow_annotation_exempts_the_comment_line(self) -> None:
        diff = _diff(
            "src/teatree/x.py",
            "    # Consolidated into !7511  # privacy-scan:allow",
        )
        result = _run(diff)
        assert result.returncode == 0, result.stdout + result.stderr
