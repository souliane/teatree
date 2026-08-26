"""Top-level group aliases resolve fully but render once (#3838).

An agent bash-permission layer refuses any command carrying a bare ``eval``
token — it reads it as the shell builtin — so ``t3 eval …`` is rejected before
it runs and no sub-agent can measure the eval suite. The alias mount gives the
same group a non-colliding spelling. These tests pin both halves: the alias
resolves every canonical path (so skill prose citing it passes the #550
registry), and the generated reference renders the alias subtree once instead
of duplicating all 44 sections.
"""

import re

import pytest

from teatree.cli import app
from teatree.cli.command_tree import COMMAND_GROUP_ALIASES
from teatree.cli_reference import command_paths, render_cli_reference_deterministic

_BASE = "t3"
_HEADING = re.compile(r"^#+ `([^`]+)`$", re.MULTILINE)


def _suffixes(paths: set[str], group: str) -> set[str]:
    prefix = f"{_BASE} {group} "
    return {path[len(prefix) :] for path in paths if path.startswith(prefix)}


@pytest.fixture(scope="module")
def paths() -> set[str]:
    return command_paths(app)


@pytest.fixture(scope="module")
def reference() -> str:
    return render_cli_reference_deterministic(app)


class TestAliasMount:
    def test_declares_at_least_one_alias(self) -> None:
        assert COMMAND_GROUP_ALIASES

    @pytest.mark.parametrize(("alias", "canonical"), sorted(COMMAND_GROUP_ALIASES.items()))
    def test_alias_resolves_every_canonical_path(self, alias: str, canonical: str, paths: set[str]) -> None:
        canonical_suffixes = _suffixes(paths, canonical)
        assert canonical_suffixes, f"`{_BASE} {canonical}` has no subcommands to alias"
        assert f"{_BASE} {alias}" in paths
        assert _suffixes(paths, alias) == canonical_suffixes


class TestAliasRendering:
    def test_alias_subtree_renders_once(self, reference: str) -> None:
        rendered = _HEADING.findall(reference)
        for alias in COMMAND_GROUP_ALIASES:
            assert rendered.count(f"{_BASE} {alias}") == 1
            assert not [name for name in rendered if name.startswith(f"{_BASE} {alias} ")]

    def test_canonical_subtree_still_renders_in_full(self, reference: str, paths: set[str]) -> None:
        rendered = set(_HEADING.findall(reference))
        for canonical in set(COMMAND_GROUP_ALIASES.values()):
            assert _suffixes(paths, canonical) <= _suffixes(rendered, canonical)

    def test_only_declared_alias_subtrees_are_collapsed(self, reference: str, paths: set[str]) -> None:
        collapsed = sum(len(_suffixes(paths, alias)) for alias in COMMAND_GROUP_ALIASES)
        assert len(_HEADING.findall(reference)) == len(paths) - collapsed
