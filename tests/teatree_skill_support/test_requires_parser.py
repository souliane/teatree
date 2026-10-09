"""``parse_requires`` / ``parse_companions`` read the ``requires:`` / ``companions:`` frontmatter lists."""

import pytest

from teatree.skill_support.requires_parser import parse_companions as teatree_parse_companions
from teatree.skill_support.requires_parser import parse_requires as teatree_parse

_PARSERS = pytest.mark.parametrize("parse", [teatree_parse], ids=["teatree"])
_COMPANION_PARSERS = pytest.mark.parametrize("parse", [teatree_parse_companions], ids=["teatree"])


@_PARSERS
class TestParseRequires:
    def test_no_frontmatter_returns_none(self, parse) -> None:
        assert parse("no frontmatter here") is None

    def test_unclosed_frontmatter_returns_none(self, parse) -> None:
        assert parse("---\nname: x\n") is None

    def test_missing_requires_returns_none(self, parse) -> None:
        assert parse("---\nname: x\ndescription: d\n---\n") is None

    def test_empty_requires_returns_empty_list(self, parse) -> None:
        assert parse("---\nname: x\nrequires:\n---\n") == []

    def test_requires_members(self, parse) -> None:
        md = "---\nname: code\nrequires:\n  - workspace\n  - architecture-design\n---\n"
        assert parse(md) == ["workspace", "architecture-design"]

    def test_requires_strips_quotes(self, parse) -> None:
        md = "---\nname: x\nrequires:\n  - 'rules'\n  - \"platforms\"\n---\n"
        assert parse(md) == ["rules", "platforms"]

    def test_requires_stops_at_next_top_level_key(self, parse) -> None:
        md = "---\nname: x\nrequires:\n  - rules\nmetadata:\n  version: 0.0.1\n---\n"
        assert parse(md) == ["rules"]

    def test_requires_after_another_list_key(self, parse) -> None:
        md = "---\nname: x\ncompatibility: any\nrequires:\n  - rules\n---\n"
        assert parse(md) == ["rules"]


@_COMPANION_PARSERS
class TestParseCompanions:
    """``parse_companions`` extracts only the ``companions:`` list — both twins agree."""

    def test_missing_companions_returns_none(self, parse) -> None:
        assert parse("---\nname: x\ndescription: d\n---\n") is None

    def test_empty_companions_returns_empty_list(self, parse) -> None:
        assert parse("---\nname: x\ncompanions:\n---\n") == []

    def test_companions_members(self, parse) -> None:
        md = "---\nname: code\ncompanions:\n  - rules\n  - writing-plans\n---\n"
        assert parse(md) == ["rules", "writing-plans"]

    def test_companions_does_not_capture_requires(self, parse) -> None:
        md = "---\nname: x\nrequires:\n  - rules\ncompanions:\n  - writing-plans\n---\n"
        assert parse(md) == ["writing-plans"]
