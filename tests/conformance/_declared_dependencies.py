"""What ``pyproject.toml`` declares, and which installed distribution each import name resolves to.

Two lanes ask that same question — ``test_import_pinned_dependency_bounds`` (is a
submodule-coupled dependency bounded?) and ``test_utils_imports_are_declared_dependencies``
(is an import-time import declared at all?). Held in two copies, the PEP 508 spec grammar
drifts in one and not the other, and the lanes then disagree about which distributions are
declared while both stay green — a blind spot no assertion in either file can see.
"""

import re
import tomllib
from functools import cache
from importlib.metadata import packages_distributions

from tests.conformance._src_tree import REPO_ROOT

PYPROJECT = REPO_ROOT / "pyproject.toml"

_SPEC_NAME_END = re.compile(r"[<>=!~@\[;\s]")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def spec_name(spec: str) -> str:
    """The canonical distribution name a PEP 508 requirement opens with, before any operator, extra, URL or marker."""
    return canonical(_SPEC_NAME_END.split(spec, maxsplit=1)[0].strip())


@cache
def declared_dependencies() -> dict[str, str]:
    """Canonical name to declaration for ``[project.dependencies]`` — extras excluded, a default install lacks them."""
    specs = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    return {spec_name(spec): spec for spec in specs}


@cache
def distributions_by_top_level() -> dict[str, frozenset[str]]:
    return {top: frozenset(canonical(dist) for dist in dists) for top, dists in packages_distributions().items()}
