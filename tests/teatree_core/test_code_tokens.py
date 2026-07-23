"""One shared detector for the code symbols / file paths both surfaces must format (#3624)."""

import re

from teatree.core.code_tokens import rewrite_code_tokens


def _mark(token: str) -> str:
    return f"[{token}]"


class TestRewriteCodeTokens:
    def test_wraps_a_file_path(self) -> None:
        assert rewrite_code_tokens("see src/teatree/loop/run.py now", _mark) == "see [src/teatree/loop/run.py] now"

    def test_wraps_a_dotted_symbol(self) -> None:
        assert rewrite_code_tokens("call teatree.core.tasks.claim", _mark) == "call [teatree.core.tasks.claim]"

    def test_leaves_a_url_alone(self) -> None:
        text = "read https://example.com/a/b.py now"
        assert rewrite_code_tokens(text, _mark) == text

    def test_leaves_ordinary_prose_alone(self) -> None:
        assert rewrite_code_tokens("a sentence about agents.", _mark) == "a sentence about agents."

    def test_extra_protected_patterns_are_honoured(self) -> None:
        text = "`src/a.py` and src/b.py"
        rewritten = rewrite_code_tokens(text, _mark, protected=(re.compile(r"`[^`\n]+`"),))
        assert rewritten == "`src/a.py` and [src/b.py]"
