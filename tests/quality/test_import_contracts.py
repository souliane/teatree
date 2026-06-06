"""Conformance ledger for the import-linter contracts (transitive/laundered import boundaries).

Sibling of ``test_chokepoints.py`` / ``test_catalog.py`` / ``test_regression_rules.py``.
tach's ``depends_on`` is a direct-edge allow-list; these contracts catch the
transitive/laundered chains and the wildcard sibling-independence invariants
tach's per-module model cannot express (BLUEPRINT §17.6.2, #724/#725).

The green-on-tree assertion is what lets ``lint-imports`` be a trusted blocking
gate and keeps main green: a real cross-boundary import on ``src/teatree`` turns
it red. The presence assertions pin the shipped contract set so a contract cannot
be silently dropped.
"""

from pathlib import Path

import pytest
from importlinter import configuration
from importlinter.application.use_cases import SUCCESS, lint_imports, read_user_options

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = str(_REPO_ROOT / "pyproject.toml")

_EXPECTED_CONTRACTS = {
    "Substrate must not import a concrete overlay",
    "Mini-loops are mutually independent",
}


@pytest.fixture(scope="module", autouse=True)
def _configured() -> None:
    configuration.configure()


@pytest.fixture(scope="module")
def contracts() -> tuple[dict[str, object], ...]:
    return tuple(read_user_options(config_filename=_CONFIG).contracts_options)


class TestConfig:
    def test_root_package_is_teatree(self) -> None:
        options = read_user_options(config_filename=_CONFIG)
        assert options.session_options["root_packages"] == ["teatree"]

    def test_contracts_are_non_empty(self, contracts: tuple[dict[str, object], ...]) -> None:
        assert contracts

    def test_every_contract_has_name_and_type(self, contracts: tuple[dict[str, object], ...]) -> None:
        for contract in contracts:
            assert contract.get("name"), contract
            assert contract.get("type"), contract

    def test_shipped_contract_set(self, contracts: tuple[dict[str, object], ...]) -> None:
        names = {contract["name"] for contract in contracts}
        assert names == _EXPECTED_CONTRACTS


class TestGreenOnTree:
    def test_lint_imports_passes(self) -> None:
        assert lint_imports(config_filename=_CONFIG, cache_dir=None) == SUCCESS
