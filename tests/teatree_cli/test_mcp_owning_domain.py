"""``t3 mcp serve`` runs where the control database's writes are legal.

The MCP server writes. When the containerized stack owns the control DB the host
server's writes are refused, and the ``t3`` shell alias cannot redirect it — Claude
Code launches it from ``.mcp.json`` by PATH lookup, which resolves the native console
script and never sees the alias. These tests pin the startup routing that closes that
gap, and pin that it stays inert on every install no container has claimed.
"""

import os
import sqlite3
from pathlib import Path

import pytest

from teatree.cli import mcp_owning_domain
from teatree.cli.mcp_owning_domain import DELEGATED_ENV_VAR, delegate_to_owning_domain, owning_domain_wrapper
from teatree.db.boundary import ControlDbBoundary

_UNRESOLVABLE = RuntimeError("no main clone on this machine")


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone whose ``deploy/t3`` wrapper exists and is executable, with a claimed DB."""
    repo = tmp_path / "teatree"
    (repo / "deploy").mkdir(parents=True)
    wrapper = repo / "deploy" / "t3"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)

    db = tmp_path / "data" / "db.sqlite3"
    db.parent.mkdir()
    sqlite3.connect(db).close()
    ControlDbBoundary(db, containerized=True).claim_for_container()

    monkeypatch.setattr(mcp_owning_domain, "CANONICAL_DB", db)
    monkeypatch.setattr("teatree.cli.mcp_owning_domain.find_main_clone", lambda: repo)
    monkeypatch.delenv("TEATREE_ROLE", raising=False)
    monkeypatch.delenv(DELEGATED_ENV_VAR, raising=False)
    return repo


class TestRoutingToTheOwningDomain:
    def test_a_host_server_is_handed_to_the_container_wrapper(self, clone: Path) -> None:
        assert owning_domain_wrapper() == clone / "deploy" / "t3"

    def test_delegation_replaces_the_process_with_the_wrapper(
        self, clone: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``execv``, not a subprocess — the client's stdio fds must pass through unrelayed."""
        replaced: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(os, "execv", lambda path, argv: replaced.append((path, argv)))

        delegate_to_owning_domain()

        assert replaced == [(str(clone / "deploy" / "t3"), [str(clone / "deploy" / "t3"), "mcp", "serve"])]

    def test_the_delegation_marker_stops_a_second_hop(self, clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Insurance against an exec loop if container detection ever failed inside one."""
        monkeypatch.setenv(DELEGATED_ENV_VAR, "1")

        assert owning_domain_wrapper() is None


class TestServingHereIsTheDefault:
    """Every uncertainty resolves to "serve natively" — the boundary guard still fails writes loud."""

    def test_an_unclaimed_database_serves_natively(self, clone: Path) -> None:
        ControlDbBoundary(mcp_owning_domain.CANONICAL_DB).claim_path.unlink()

        assert owning_domain_wrapper() is None

    def test_the_container_never_delegates_to_itself(self, clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_ROLE", "worker")

        assert owning_domain_wrapper() is None

    def test_a_missing_wrapper_serves_natively(self, clone: Path) -> None:
        (clone / "deploy" / "t3").unlink()

        assert owning_domain_wrapper() is None

    def test_a_non_executable_wrapper_serves_natively(self, clone: Path) -> None:
        (clone / "deploy" / "t3").chmod(0o644)

        assert owning_domain_wrapper() is None

    def test_an_unresolvable_clone_serves_natively(self, clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_clone() -> Path:
            raise _UNRESOLVABLE

        monkeypatch.setattr("teatree.cli.mcp_owning_domain.find_main_clone", no_clone)

        assert owning_domain_wrapper() is None

    def test_delegation_is_a_no_op_when_serving_here(self, clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (clone / "deploy" / "t3").unlink()
        monkeypatch.setattr(os, "execv", lambda path, argv: pytest.fail(f"delegated to {path} {argv}"))

        delegate_to_owning_domain()
