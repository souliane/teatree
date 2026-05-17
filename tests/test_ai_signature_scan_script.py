"""Integration tests for ``scripts/ai_signature_scan.py`` (#836, gate 15).

The "No AI Signature on Posts Made on the User's Behalf" rule lived only
as prose in ``/t3:rules`` and was UNENFORCED at the PR-body / commit-message
layer — PR #831 leaked the banned ``Generated with [Claude Code]`` trailer
and it was caught only by cold review. This gate makes the rule
deterministic code: the scanner refuses any PR body or commit message
carrying an AI-signature / banned trailer.

The matcher must target trailer/footer *position* and commit-message
*structure*, not a bare substring — a doc that *describes* the banned
trailer (the rule's own definition) must NOT trip the gate. These tests
invoke the script the same way ``ToolRunner.run_script`` does so the
entrypoint is exercised, not mocked.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ai_signature_scan.py"


def _run(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class TestBlocksRealViolations:
    def test_emoji_robot_generated_with_footer_blocks(self) -> None:
        body = "fix: something real\n\nA proper description.\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Generated with" in result.stdout

    def test_co_authored_by_claude_trailer_blocks(self) -> None:
        body = "fix: x\n\nbody line\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Co-Authored-By" in result.stdout

    def test_co_authored_by_anthropic_model_trailer_blocks(self) -> None:
        body = "feat: y\n\nCo-authored-by: Claude Opus 4.7 <noreply@anthropic.com>\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_generated_with_claude_code_no_emoji_blocks(self) -> None:
        body = "title\n\nGenerated with [Claude Code]\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_via_claude_footer_blocks(self) -> None:
        body = "Some PR description.\n\nSent using Claude\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_via_claude_phrase_footer_blocks(self) -> None:
        body = "Body text here.\n\nposted via Claude\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr


class TestMarkdownPrefixedTrailersBlock:
    """Cold-review finding 1 — markdown-prefix bypass.

    A markdown blockquote/list prefix must not smuggle a banned trailer
    past the line-leading anchor. ``> Co-Authored-By: …`` (quoted reply
    / AI footer), ``- Generated with …`` / ``* …`` / ``+ …`` (list
    item, common PR-template shape) are real false-negatives unless the
    leading markdown marker is stripped before anchoring the match.
    """

    def test_blockquoted_co_authored_by_blocks(self) -> None:
        body = "fix: x\n\nbody\n\n> Co-Authored-By: Claude <noreply@anthropic.com>\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Co-Authored-By" in result.stdout

    def test_double_blockquoted_via_claude_blocks(self) -> None:
        body = "Some description.\n\n>> via Claude\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_dash_list_generated_with_blocks(self) -> None:
        body = "title\n\n- Generated with [Claude Code]\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_star_list_generated_with_blocks(self) -> None:
        body = "title\n\n* Generated with [Claude Code]\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_plus_list_emoji_bot_footer_blocks(self) -> None:
        body = "title\n\n+ \U0001f916 Generated with [Claude Code]\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr


class TestViaClauseDoesNotBlockBodyProse:
    """Cold-review finding 3 — over-broad via-claude pattern.

    ``with/via/using claude`` in running body prose is legitimate; only
    a footer-position occurrence is banned.
    """

    def test_reviewed_design_with_claude_prose_passes(self) -> None:
        body = (
            "fix(core): tighten the merge gate\n\n"
            "Reviewed the design with Claude before settling on the "
            "expected_head_oid approach.\n\nRelates-to #836\n"
        )
        result = _run(body)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_real_via_claude_footer_still_blocks(self) -> None:
        body = "A proper description.\n\nvia Claude\n"
        result = _run(body)
        assert result.returncode == 1, result.stdout + result.stderr


class TestAllowsCleanCases:
    def test_clean_pr_body_passes(self) -> None:
        body = (
            "fix(core): close the TOCTOU window in the merge loop\n\n"
            "Re-bind the merge call to the verified SHA.\n\nRelates-to #836\n"
        )
        result = _run(body)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_empty_body_passes(self) -> None:
        result = _run("")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_co_authored_by_human_passes(self) -> None:
        body = "fix: pair work\n\nCo-Authored-By: Jane Dev <jane@example.com>\n"
        result = _run(body)
        assert result.returncode == 0, result.stdout + result.stderr


class TestDocsDescribingTheRuleDoNotTrip:
    """The matcher targets trailer/footer position, not a bare substring.

    A doc (this gate's own definition, /t3:rules, or BLUEPRINT) that
    *describes* the banned trailer in running prose must not self-block —
    otherwise the rule's own documentation cannot be committed.
    """

    def test_rule_definition_prose_passes(self) -> None:
        body = (
            "## No AI Signature on Posts Made on the User's Behalf\n\n"
            "Never add an AI-signature trailer such as `Co-Authored-By:` "
            "naming a model, or a `Generated with [Claude Code]` footer, "
            "to a PR body or commit message. This rule is enforced by the "
            "ai-sig-scan gate.\n\nRelates-to #836\n"
        )
        result = _run(body)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_inline_backticked_mention_passes(self) -> None:
        body = "Document that `\U0001f916 Generated with` in backticks is the banned footer we reject.\n"
        result = _run(body)
        assert result.returncode == 0, result.stdout + result.stderr
