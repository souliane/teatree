"""Fixtures shared by the quality-module mirror tests."""

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

Planter = Callable[[str, str], ModuleType]


@pytest.fixture
def planted(tmp_path: Path) -> Iterator[Planter]:
    """Import a throwaway top-level module from source, so a lookup reads a real object.

    Resolution is by dotted name through ``sys.path``, so a fixture written to disk is the
    only way to exercise it — a `types.ModuleType` built in memory is never importable.
    """
    names: list[str] = []

    def plant(name: str, source: str) -> ModuleType:
        (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
        sys.path.insert(0, str(tmp_path))
        importlib.invalidate_caches()
        try:
            module = importlib.import_module(name)
        finally:
            sys.path.remove(str(tmp_path))
        names.append(name)
        return module

    yield plant
    for name in names:
        sys.modules.pop(name, None)
