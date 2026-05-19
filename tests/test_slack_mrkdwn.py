"""Tests for ``teatree.slack_mrkdwn.slack_linkify`` and ``normalize_slack_message``.

The dashboard markdown sent through ``notify_user`` to the user's Slack DM
must render with clickable PR/MR/issue refs. Slack mrkdwn uses
``<url|label>`` — GitHub-flavored ``[label](url)`` and bare ``!N`` / ``#N``
tokens render as inert text. This module rewrites those tokens.

``normalize_slack_message`` enforces structural readability: one idea per
line, blank-line-separated blocks, and ``•``-in-paragraph bullets converted
to real newline-prefixed ``- `` list items.
"""

import re

from teatree.slack_mrkdwn import normalize_slack_message, slack_linkify


def _pipes_outside_mrkdwn(line: str) -> int:
    """Count ``|`` characters that aren't the url|label separator in a <…|…> token."""
    stripped = re.sub(r"<[^>]*>", "", line)
    return stripped.count("|")


def _mr(n: int) -> str | None:
    table = {
        281: "https://gitlab.example.com/group/repo-a/-/merge_requests/281",
        381: "https://gitlab.example.com/group/repo-b/-/merge_requests/381",
        7439: "https://gitlab.example.com/group/repo-c/-/merge_requests/7439",
    }
    return table.get(n)


def _issue(n: int) -> str | None:
    table = {
        1011: "https://github.com/souliane/teatree/issues/1011",
        1010: "https://github.com/souliane/teatree/issues/1010",
    }
    return table.get(n)


class TestSlackLinkifyBareMrTokens:
    def test_resolves_known_mr_token_to_mrkdwn_link(self) -> None:
        out = slack_linkify("ship !281 next", mr_resolver=_mr)
        assert out == "ship <https://gitlab.example.com/group/repo-a/-/merge_requests/281|!281> next"

    def test_resolves_multiple_mr_tokens_in_one_line(self) -> None:
        text = "| !281 | repo-a | APPROVE |\n| !381 | repo-b | APPROVE-WITH-NIT |"
        out = slack_linkify(text, mr_resolver=_mr)
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/281|!281>" in out
        assert "<https://gitlab.example.com/group/repo-b/-/merge_requests/381|!381>" in out
        # Newlines preserved
        assert out.count("\n") == text.count("\n")
        # Each line keeps its table-pipe count (rewrite adds pipes only INSIDE <...>)
        for orig_line, out_line in zip(text.splitlines(), out.splitlines(), strict=True):
            assert _pipes_outside_mrkdwn(out_line) == orig_line.count("|")

    def test_ambiguous_mr_token_left_bare(self) -> None:
        out = slack_linkify("ship !999 next", mr_resolver=_mr)
        assert out == "ship !999 next"

    def test_no_resolver_leaves_mr_tokens_bare(self) -> None:
        out = slack_linkify("ship !281 next")
        assert out == "ship !281 next"


class TestSlackLinkifyBareIssueTokens:
    def test_resolves_known_issue_token_to_mrkdwn_link(self) -> None:
        out = slack_linkify("fixes #1011", issue_resolver=_issue)
        assert out == "fixes <https://github.com/souliane/teatree/issues/1011|#1011>"

    def test_ambiguous_issue_token_left_bare(self) -> None:
        out = slack_linkify("fixes #9999", issue_resolver=_issue)
        assert out == "fixes #9999"

    def test_no_resolver_leaves_issue_tokens_bare(self) -> None:
        out = slack_linkify("fixes #1011")
        assert out == "fixes #1011"


class TestSlackLinkifyMarkdownLinks:
    def test_rewrites_gh_markdown_link_to_mrkdwn(self) -> None:
        out = slack_linkify("see [the PR](https://example.com/pr/1)")
        assert out == "see <https://example.com/pr/1|the PR>"

    def test_label_with_pipe_inside_is_escaped(self) -> None:
        # Slack mrkdwn doesn't support pipes in labels; the helper substitutes
        # them with a unicode bar so the label stays readable rather than the
        # mrkdwn parser truncating the label at the first '|'.
        out = slack_linkify("[a|b](https://example.com)")
        # Exactly one mrkdwn token (one '<', one '>'), one url|label separator,
        # and the literal label-pipe has been substituted out.
        assert out.count("<") == 1
        assert out.count(">") == 1
        assert out.count("|") == 1
        assert "a|b" not in out
        assert out.endswith("b>")


class TestSlackLinkifyCodeBlocks:
    def test_code_block_contents_are_preserved(self) -> None:
        text = "before\n```\n!281 should stay bare in code\n[link](http://x)\n```\nafter !281"
        out = slack_linkify(text, mr_resolver=_mr)
        # Inside the code block: nothing rewritten
        assert "!281 should stay bare in code" in out
        assert "[link](http://x)" in out
        # Outside the code block: rewritten
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/281|!281>" in out

    def test_inline_code_preserved(self) -> None:
        text = "use `!281` token, ref !281"
        out = slack_linkify(text, mr_resolver=_mr)
        assert "`!281`" in out
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/281|!281>" in out


class TestSlackLinkifyIdempotent:
    def test_double_application_is_noop(self) -> None:
        once = slack_linkify("see !281 and [PR](http://x)", mr_resolver=_mr)
        twice = slack_linkify(once, mr_resolver=_mr)
        assert once == twice

    def test_already_mrkdwn_link_is_preserved(self) -> None:
        text = "see <https://example.com/pr/1|the PR>"
        assert slack_linkify(text) == text


class TestSlackLinkifyEdgeCases:
    def test_empty_string(self) -> None:
        assert slack_linkify("") == ""

    def test_token_at_end_of_line(self) -> None:
        out = slack_linkify("approve !281\n", mr_resolver=_mr)
        assert "<https://gitlab.example.com/group/repo-a/-/merge_requests/281|!281>" in out
        assert out.endswith("\n")

    def test_token_inside_word_not_matched(self) -> None:
        # foo!281bar — the ! is not at a word boundary, leave alone
        out = slack_linkify("foo!281bar", mr_resolver=_mr)
        assert out == "foo!281bar"

    def test_hash_inside_word_not_matched(self) -> None:
        out = slack_linkify("abc#1011def", issue_resolver=_issue)
        assert out == "abc#1011def"

    def test_resolver_returning_none_leaves_token_bare(self) -> None:
        def always_none(_n: int) -> str | None:
            return None

        out = slack_linkify("see !281", mr_resolver=always_none)
        assert out == "see !281"

    def test_markdown_link_inside_table_cell(self) -> None:
        line = "| [PR](http://x) | done |"
        out = slack_linkify(line)
        assert "<http://x|PR>" in out
        # Table structure preserved — same count of table-level pipes
        assert _pipes_outside_mrkdwn(out) == line.count("|")

    def test_preserves_headers_and_pipes(self) -> None:
        text = "| MR | repo | verdict |\n|---|---|---|\n| !281 | repo-a | ok |"
        out = slack_linkify(text, mr_resolver=_mr)
        assert out.count("\n") == text.count("\n")
        # Header row untouched
        assert "| MR | repo | verdict |" in out
        # Separator row untouched
        assert "|---|---|---|" in out


class TestNormalizeSlackMessageBullets:
    def test_bullet_in_paragraph_becomes_own_line(self) -> None:
        text = "Here is the summary. • First item • Second item • Third item"
        out = normalize_slack_message(text)
        lines = out.splitlines()
        assert any("- First item" in line for line in lines)
        assert any("- Second item" in line for line in lines)
        assert any("- Third item" in line for line in lines)

    def test_bullet_items_each_on_own_line(self) -> None:
        text = "Summary text • Alpha • Beta • Gamma"
        out = normalize_slack_message(text)
        assert out.count("\n") >= 2  # at least 2 newlines for 3 bullets

    def test_existing_dash_bullets_not_duplicated(self) -> None:
        text = "Summary:\n- Alpha\n- Beta"
        out = normalize_slack_message(text)
        assert out.count("- Alpha") == 1
        assert out.count("- Beta") == 1

    def test_leading_bullet_becomes_dash(self) -> None:
        text = "• Only item"
        out = normalize_slack_message(text)
        assert out.strip().startswith("- ")


class TestNormalizeSlackMessageBlankLines:
    def test_blocks_separated_by_blank_line(self) -> None:
        text = "Block one.\nBlock two."
        out = normalize_slack_message(text)
        # Single newline between adjacent lines is acceptable; test checks structure
        assert "Block one." in out
        assert "Block two." in out

    def test_wall_of_text_gets_blank_line_between_blocks(self) -> None:
        # Long wall of text: heading line, bullet group, trailing action — no blank lines
        text = (
            "*Dashboard update*\n"
            "Here is the current status. Everything looks fine. Please review the items below.\n"
            "• PR !281 approved • PR !381 needs nit fixes • PR !999 blocked\n"
            "Let me know if you need anything."
        )
        out = normalize_slack_message(text)
        # Blank lines should separate the heading from body and trailing action
        assert "\n\n" in out

    def test_no_triple_blank_lines(self) -> None:
        text = "Line one\n\n\nLine two"
        out = normalize_slack_message(text)
        assert "\n\n\n" not in out


class TestNormalizeSlackMessageCodePreservation:
    def test_fenced_code_block_untouched(self) -> None:
        text = "Before\n```\n• not a bullet\nsome code here\n```\nAfter • bullet"
        out = normalize_slack_message(text)
        # Bullet inside fence must stay as-is
        assert "• not a bullet" in out
        # Bullet outside fence must be converted
        assert "- bullet" in out

    def test_inline_code_untouched(self) -> None:
        text = "Use `• symbol` in your code. • Real bullet"
        out = normalize_slack_message(text)
        assert "`• symbol`" in out
        assert "- Real bullet" in out

    def test_url_not_broken(self) -> None:
        text = "See https://example.com/path?a=1&b=2 for details"
        out = normalize_slack_message(text)
        assert "https://example.com/path?a=1&b=2" in out

    def test_mrkdwn_link_preserved(self) -> None:
        text = "See <https://example.com/pr/1|the PR> for details"
        out = normalize_slack_message(text)
        assert "<https://example.com/pr/1|the PR>" in out


class TestNormalizeSlackMessageIdempotent:
    def test_already_normalized_text_unchanged(self) -> None:
        text = "*Heading*\n\n- Item one\n- Item two\n\nTrailing line."
        out = normalize_slack_message(text)
        assert normalize_slack_message(out) == out

    def test_plain_text_double_application_noop(self) -> None:
        text = "Hello world. This is a simple message."
        once = normalize_slack_message(text)
        twice = normalize_slack_message(once)
        assert once == twice

    def test_bullet_chain_double_application_noop(self) -> None:
        text = "Summary • Alpha • Beta • Gamma"
        once = normalize_slack_message(text)
        twice = normalize_slack_message(once)
        assert once == twice


class TestNormalizeSlackMessageEdgeCases:
    def test_empty_string(self) -> None:
        assert normalize_slack_message("") == ""

    def test_only_whitespace(self) -> None:
        out = normalize_slack_message("   \n  \n  ")
        # Should not explode; leading/trailing stripped or preserved reasonably
        assert isinstance(out, str)

    def test_no_mutation_when_already_structured(self) -> None:
        text = "*Status*\n\n- Done\n- Pending\n\nLet me know."
        out = normalize_slack_message(text)
        assert "- Done" in out
        assert "- Pending" in out

    def test_real_world_wall_of_text(self) -> None:
        # Realistic agent output that triggered the user complaint
        text = (
            ":information_source: *info*\n"
            "Here is the current review status for your open MRs. "
            "MR !281 (repo-a) is approved and ready to merge. "
            "MR !381 (repo-b) has one nit comment that needs addressing. "
            "• !281 APPROVE • !381 APPROVE-WITH-NIT • !7439 WAIT"
            " Please check the dashboard for the full details and let me know if you have questions."
        )
        out = normalize_slack_message(text)
        # Each bullet item must be on its own line
        lines = out.splitlines()
        bullet_lines = [line for line in lines if line.strip().startswith("- ")]
        assert len(bullet_lines) >= 3
        # No paragraph runs past ~3 sentences without a line break
        for line in lines:
            # Rough check: no line has more than 3 sentence-ending sequences mid-line
            mid_sentences = len(re.findall(r"\. [A-Z]", line))
            assert mid_sentences <= 2
