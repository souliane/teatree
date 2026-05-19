"""Tests for ``teatree.slack_mrkdwn.slack_linkify``.

The dashboard markdown sent through ``notify_user`` to the user's Slack DM
must render with clickable PR/MR/issue refs. Slack mrkdwn uses
``<url|label>`` — GitHub-flavored ``[label](url)`` and bare ``!N`` / ``#N``
tokens render as inert text. This module rewrites those tokens.
"""

import re

from teatree.slack_mrkdwn import slack_linkify


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
