"""Conformance: the Anthropic cache-pricing multipliers live in exactly one module.

The read (0.1x) and write (1.25x) cache multipliers are a fixed property of the
Anthropic API, not of any model. They were once defined independently in three
places (``core.cost``, ``eval.cost_fit``, ``eval.models``); the SSOT is
:mod:`teatree.pricing`. This gate greps ``src/teatree`` for any other definition
so a fourth copy can never re-accrete.
"""

import re
from pathlib import Path

import pytest

from teatree.pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER

_SRC = Path(__file__).resolve().parents[1] / "src" / "teatree"
_SSOT = _SRC / "pricing.py"

#: An assignment of a name that ends in ``CACHE_READ_MULTIPLIER`` or
#: ``CACHE_WRITE_MULTIPLIER`` (with or without a leading underscore prefix).
_DEFINITION = re.compile(r"^_?CACHE_(?:READ|WRITE)_MULTIPLIER\s*=", re.MULTILINE)


def _definition_sites() -> list[Path]:
    return [path for path in _SRC.rglob("*.py") if _DEFINITION.search(path.read_text(encoding="utf-8"))]


class TestSingleSource:
    def test_multipliers_defined_in_exactly_one_module(self) -> None:
        sites = _definition_sites()
        assert sites == [_SSOT], "cache multipliers must be defined only in teatree.pricing; found " + ", ".join(
            str(p.relative_to(_SRC.parents[1])) for p in sites
        )


class TestValues:
    def test_read_multiplier_is_one_tenth(self) -> None:
        assert pytest.approx(0.1) == CACHE_READ_MULTIPLIER

    def test_write_multiplier_is_one_and_a_quarter(self) -> None:
        assert pytest.approx(1.25) == CACHE_WRITE_MULTIPLIER
