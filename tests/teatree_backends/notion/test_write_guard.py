"""The Notion write guard: a write lands only under a configured root, and a denied root always wins.

Notion's capabilities cover the whole integration and its API cannot say which pages a customer
sees, so the boundary is configuration. Reads stay unrestricted; this pins the write side.
"""

from itertools import pairwise
from unittest.mock import patch

import pytest

from teatree.backends.notion import write_guard
from teatree.backends.notion.errors import NotionError, NotionNotSharedError, NotionWriteRefusedError
from teatree.backends.notion.write_guard import WriteGuard, WriteScope
from teatree.config.settings import UserSettings

_SECTION = "22222222222222222222222222222222"
_PAGE = "11111111111111111111111111111111"
_INTERNAL = "33333333333333333333333333333333"
_CUSTOMER = "44444444444444444444444444444444"


def _dashed(raw: str) -> str:
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


class _Tree:
    def __init__(self, parents: dict[str, str | None]) -> None:
        self._parents = {child: _dashed(parent) if parent else None for child, parent in parents.items()}
        self.lookups: list[str] = []

    def parent_of(self, node: str) -> str | None:
        self.lookups.append(node)
        return self._parents[node.replace("-", "")]


_CHAIN = {_SECTION: _PAGE, _PAGE: _INTERNAL, _INTERNAL: None}


def _guard(tree: _Tree, *, allowed: list[str] | None = None, denied: list[str] | None = None, clock=None) -> WriteGuard:
    scope = WriteScope.of(allowed=allowed or [], denied=denied or [])
    return WriteGuard(parent_of=tree.parent_of, scope=lambda: scope, clock=clock or (lambda: 0.0))


def _subclasses(root: type) -> list[type]:
    found = []
    for child in root.__subclasses__():
        found += [child, *_subclasses(child)]
    return found


class TestDefaultDeny:
    def test_a_venue_with_no_root_configured_refuses_every_write(self) -> None:
        with pytest.raises(NotionWriteRefusedError, match="none of its ancestors is a write-allowed root"):
            _guard(_Tree(_CHAIN)).check(_SECTION)

    def test_a_write_under_an_allowed_root_passes(self) -> None:
        _guard(_Tree(_CHAIN), allowed=[_INTERNAL]).check(_SECTION)

    def test_the_allowed_root_itself_is_writable(self) -> None:
        _guard(_Tree(_CHAIN), allowed=[_INTERNAL]).check(_INTERNAL)

    def test_a_root_matches_whatever_form_it_was_configured_in(self) -> None:
        _guard(_Tree(_CHAIN), allowed=[f"https://www.notion.so/Internal-{_INTERNAL}"]).check(_dashed(_SECTION))


class TestADeniedRootAlwaysWins:
    def test_a_denied_page_under_an_allowed_root_refuses_and_names_the_denied_root(self) -> None:
        with pytest.raises(NotionWriteRefusedError, match=_dashed(_PAGE)):
            _guard(_Tree(_CHAIN), allowed=[_INTERNAL], denied=[_PAGE]).check(_SECTION)

    def test_a_denied_root_above_an_allowed_one_refuses(self) -> None:
        with pytest.raises(NotionWriteRefusedError, match=_dashed(_INTERNAL)):
            _guard(_Tree(_CHAIN), allowed=[_PAGE], denied=[_INTERNAL]).check(_SECTION)


class TestAChainThatCannotBeResolvedRefuses:
    def test_a_parent_that_cannot_be_read_refuses(self) -> None:
        def unreadable(node: str) -> str | None:
            raise NotionNotSharedError(node)

        guard = WriteGuard(parent_of=unreadable, scope=lambda: WriteScope.of(allowed=[_SECTION], denied=[]))

        with pytest.raises(NotionWriteRefusedError, match="could not be read"):
            guard.check(_SECTION)

    def test_a_parent_chain_that_loops_refuses(self) -> None:
        with pytest.raises(NotionWriteRefusedError, match="loops"):
            _guard(_Tree({_SECTION: _PAGE, _PAGE: _SECTION}), allowed=[_INTERNAL]).check(_SECTION)

    def test_a_chain_deeper_than_the_cap_refuses(self) -> None:
        ids = [f"{index:032x}" for index in range(1, 100)]
        deep: dict[str, str | None] = dict(pairwise(ids))
        deep[ids[-1]] = None

        with pytest.raises(NotionWriteRefusedError, match="deeper"):
            _guard(_Tree(deep), allowed=[ids[-1]]).check(ids[0])


class TestAncestorsAreCachedWithABoundedLifetime:
    def test_a_second_write_within_the_lifetime_reads_no_parent_again(self) -> None:
        tree = _Tree(_CHAIN)
        guard = _guard(tree, allowed=[_INTERNAL])

        guard.check(_SECTION)
        first = len(tree.lookups)
        guard.check(_SECTION)

        assert len(tree.lookups) == first

    def test_a_parent_is_read_again_once_its_lifetime_lapses(self) -> None:
        tree = _Tree(_CHAIN)
        now = [0.0]
        guard = _guard(tree, allowed=[_INTERNAL], clock=lambda: now[0])

        guard.check(_SECTION)
        first = len(tree.lookups)
        now[0] = 24 * 3600.0
        guard.check(_SECTION)

        assert len(tree.lookups) == 2 * first


class TestTheRefusalIsItsOwnOutcome:
    def test_no_other_notion_error_exits_with_its_code(self) -> None:
        codes = [error.exit_code for error in _subclasses(NotionError)]

        assert codes.count(NotionWriteRefusedError.exit_code) == 1


class TestTheScopeComesFromSettings:
    def test_both_root_lists_are_read_for_the_overlay(self) -> None:
        with patch.object(write_guard, "notion_write_roots", return_value=([_INTERNAL], [_CUSTOMER])) as roots:
            scope = WriteScope.for_overlay("acme-overlay")

        roots.assert_called_once_with("acme-overlay")
        assert scope == WriteScope.of(allowed=[_INTERNAL], denied=[_CUSTOMER])

    def test_the_shipped_defaults_configure_no_root(self) -> None:
        defaults = UserSettings()

        assert defaults.notion_write_allowed_roots == []
        assert defaults.notion_write_denied_roots == []
