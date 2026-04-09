"""Tests for scripts/lib/trigger_parser — the single source of truth for SKILL.md trigger parsing."""

import sys
from pathlib import Path

import pytest

# Add scripts/lib to path so we can import the standalone module.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib.trigger_parser import _parse_trigger_line, parse_triggers  # noqa: E402


class TestParseTriggers:
    def test_full_triggers(self):
        md = (
            "---\nname: t3-ship\ntriggers:\n  priority: 10\n  exclude: '\\breview\\b'\n"
            "  keywords:\n    - '\\bcommit\\b'\n    - '\\bpush\\b'\n  urls:\n"
            "    - 'https?://example.com'\n---\n# Ship"
        )
        result = parse_triggers(md)
        assert result is not None
        assert result["priority"] == 10
        assert result["exclude"] == r"\breview\b"
        assert result["keywords"] == [r"\bcommit\b", r"\bpush\b"]
        assert result["urls"] == ["https?://example.com"]
        assert result["end_of_session"] is False

    def test_end_of_session(self):
        md = (
            "---\nname: t3-retro\ntriggers:\n  priority: 100\n"
            "  end_of_session: true\n  keywords:\n    - '\\bretro\\b'\n---\n"
        )
        result = parse_triggers(md)
        assert result is not None
        assert result["end_of_session"] is True

    def test_no_triggers(self):
        assert parse_triggers("---\nname: t3-rules\n---\n# Rules") is None

    def test_no_frontmatter(self):
        assert parse_triggers("# No frontmatter") is None

    def test_no_closing_fence(self):
        assert parse_triggers("---\nname: test\ntriggers:\n  keywords:\n") is None

    def test_default_priority(self):
        md = "---\nname: test\ntriggers:\n  keywords:\n    - '\\btest\\b'\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["priority"] == 50

    def test_triggers_block_terminated_by_next_key(self):
        md = "---\nname: test\ntriggers:\n  priority: 5\n  keywords:\n    - '\\bfoo\\b'\nmetadata:\n  version: 1\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["keywords"] == [r"\bfoo\b"]

    def test_all_fields(self):
        md = (
            "---\nname: test\ntriggers:\n  priority: 5\n  exclude: '\\bno\\b'\n"
            "  end_of_session: true\n  keywords:\n    - '\\bfoo\\b'\n"
            "  urls:\n    - 'https://example.com'\nmetadata:\n  version: 1\n---\n"
        )
        result = parse_triggers(md)
        assert result is not None
        assert result["priority"] == 5
        assert result["exclude"] == r"\bno\b"
        assert result["end_of_session"] is True
        assert result["keywords"] == [r"\bfoo\b"]
        assert result["urls"] == ["https://example.com"]

    def test_search_hints_standalone(self):
        md = "---\nname: test\nsearch_hints:\n  - debug\n  - troubleshoot\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["search_hints"] == ["debug", "troubleshoot"]

    def test_search_hints_with_triggers(self):
        md = (
            "---\nname: test\ntriggers:\n  priority: 10\n  keywords:\n"
            "    - '\\bcommit\\b'\nsearch_hints:\n  - ship\n  - deliver\n"
            "metadata:\n  version: 1\n---\n"
        )
        result = parse_triggers(md)
        assert result is not None
        assert result["keywords"] == [r"\bcommit\b"]
        assert result["search_hints"] == ["ship", "deliver"]
        assert result["priority"] == 10

    def test_search_hints_before_triggers(self):
        md = "---\nname: test\nsearch_hints:\n  - fix\n  - error\ntriggers:\n  keywords:\n    - '\\bdebug\\b'\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["search_hints"] == ["fix", "error"]
        assert result["keywords"] == [r"\bdebug\b"]

    def test_no_search_hints_returns_empty_list(self):
        md = "---\nname: test\ntriggers:\n  keywords:\n    - '\\btest\\b'\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["search_hints"] == []


class TestParseRequires:
    def test_requires_standalone(self):
        md = "---\nname: t3-review\nrequires:\n  - workspace\n  - platforms\n  - code\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["requires"] == ["workspace", "platforms", "code"]

    def test_no_requires_returns_empty_list(self):
        md = "---\nname: test\ntriggers:\n  keywords:\n    - '\\btest\\b'\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["requires"] == []

    def test_requires_with_triggers(self):
        md = (
            "---\nname: t3-ship\ntriggers:\n  priority: 10\n  keywords:\n"
            "    - '\\bcommit\\b'\nrequires:\n  - workspace\n  - rules\n---\n"
        )
        result = parse_triggers(md)
        assert result is not None
        assert result["keywords"] == [r"\bcommit\b"]
        assert result["requires"] == ["workspace", "rules"]
        assert result["priority"] == 10

    def test_requires_before_triggers(self):
        md = "---\nname: test\nrequires:\n  - rules\ntriggers:\n  keywords:\n    - '\\bdebug\\b'\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["requires"] == ["rules"]
        assert result["keywords"] == [r"\bdebug\b"]

    def test_requires_only_marks_found(self):
        md = "---\nname: test\nrequires:\n  - workspace\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["requires"] == ["workspace"]


class TestParseCompanions:
    def test_companions_standalone(self):
        md = "---\nname: t3-code\ncompanions:\n  - test-driven-development\n  - verification-before-completion\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["companions"] == ["test-driven-development", "verification-before-completion"]

    def test_no_companions_returns_empty_list(self):
        md = "---\nname: test\ntriggers:\n  keywords:\n    - '\\btest\\b'\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["companions"] == []

    def test_companions_with_requires_and_triggers(self):
        md = (
            "---\nname: t3-ship\ntriggers:\n  priority: 10\n  keywords:\n"
            "    - '\\bcommit\\b'\nrequires:\n  - workspace\n  - rules\n"
            "companions:\n  - finishing-a-development-branch\n---\n"
        )
        result = parse_triggers(md)
        assert result is not None
        assert result["keywords"] == [r"\bcommit\b"]
        assert result["requires"] == ["workspace", "rules"]
        assert result["companions"] == ["finishing-a-development-branch"]
        assert result["priority"] == 10

    def test_companions_before_requires(self):
        md = "---\nname: test\ncompanions:\n  - skill-a\nrequires:\n  - rules\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["companions"] == ["skill-a"]
        assert result["requires"] == ["rules"]

    def test_companions_only_marks_found(self):
        md = "---\nname: test\ncompanions:\n  - writing-plans\n---\n"
        result = parse_triggers(md)
        assert result is not None
        assert result["companions"] == ["writing-plans"]


class TestParseTriggerLine:
    def test_priority(self):
        triggers: dict = {"priority": 50, "keywords": [], "urls": [], "exclude": "", "end_of_session": False}
        assert _parse_trigger_line("priority: 10", triggers, "") == ""
        assert triggers["priority"] == 10

    def test_exclude(self):
        triggers: dict = {"priority": 50, "keywords": [], "urls": [], "exclude": "", "end_of_session": False}
        assert _parse_trigger_line("exclude: '\\bx\\b'", triggers, "") == ""
        assert triggers["exclude"] == r"\bx\b"

    def test_end_of_session(self):
        triggers: dict = {"priority": 50, "keywords": [], "urls": [], "exclude": "", "end_of_session": False}
        assert _parse_trigger_line("end_of_session: true", triggers, "") == ""
        assert triggers["end_of_session"] is True

    def test_keywords_and_urls_keys(self):
        triggers: dict = {"priority": 50, "keywords": [], "urls": [], "exclude": "", "end_of_session": False}
        assert _parse_trigger_line("keywords:", triggers, "") == "keywords"
        assert _parse_trigger_line("urls:", triggers, "") == "urls"

    def test_list_items(self):
        triggers: dict = {"priority": 50, "keywords": [], "urls": [], "exclude": "", "end_of_session": False}
        assert _parse_trigger_line("- '\\bfoo\\b'", triggers, "keywords") == "keywords"
        assert triggers["keywords"] == [r"\bfoo\b"]
        assert _parse_trigger_line("- 'https://x'", triggers, "urls") == "urls"
        assert triggers["urls"] == ["https://x"]

    @pytest.mark.parametrize("line", ["something_else", "random: value", ""])
    def test_non_matching_preserves_current_key(self, line):
        triggers: dict = {"priority": 50, "keywords": [], "urls": [], "exclude": "", "end_of_session": False}
        assert _parse_trigger_line(line, triggers, "keywords") == "keywords"
