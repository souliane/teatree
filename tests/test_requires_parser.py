"""``parse_requires`` extracts only the ``requires:`` list from SKILL.md frontmatter.

Both the standalone hook copy (``scripts/lib/requires_parser.py``) and the
teatree copy (``teatree.skill_support.requires_parser``) must behave
identically — the hook needs a no-teatree-import twin, so they are parsed under
one parametrized suite.
"""

import sys
from pathlib import Path

import pytest

from teatree.skill_support.requires_parser import parse_requires as teatree_parse

_SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from lib.requires_parser import parse_requires as hook_parse  # noqa: E402

_PARSERS = pytest.mark.parametrize("parse", [teatree_parse, hook_parse], ids=["teatree", "hook"])


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
