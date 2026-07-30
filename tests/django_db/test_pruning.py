"""Tests for teatree.utils.django_db — DSLR snapshot parsing and pruning.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from subprocess import CompletedProcess

import pytest

from teatree.utils import run as run_mod
from teatree.utils.django_db import dslr as dslr_mod
from teatree.utils.django_db import prune_dslr_snapshots
from teatree.utils.django_db.dslr_prune import parse_dslr_snapshots as _parse_dslr_snapshots


class TestParseDslrSnapshots:
    def test_groups_by_tenant(self) -> None:
        stdout = (
            "20260402_development-acme  125MB\n"
            "20260401_development-acme  123MB\n"
            "20260315_development-tenant-b  98MB\n"
            "20260320_development-tenant-b  100MB\n"
        )
        result = _parse_dslr_snapshots(stdout)
        assert set(result) == {"development-acme", "development-tenant-b"}
        assert result["development-acme"] == [
            "20260402_development-acme",
            "20260401_development-acme",
        ]
        assert result["development-tenant-b"] == [
            "20260320_development-tenant-b",
            "20260315_development-tenant-b",
        ]

    def test_empty_output(self) -> None:
        assert _parse_dslr_snapshots("") == {}

    def test_skips_blank_lines(self) -> None:
        result = _parse_dslr_snapshots("\n\n20260401_dev-acme  50MB\n\n")
        assert result == {"dev-acme": ["20260401_dev-acme"]}


class TestPruneDslrSnapshots:
    def test_deletes_old_keeps_newest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dslr_output = (
            "20260402_development-acme  125MB\n20260401_development-acme  123MB\n20260320_development-acme  120MB\n"
        )
        deleted: list[str] = []

        def fake_run(cmd, **kw):
            if "delete" in cmd:
                deleted.append(cmd[-1])
            return CompletedProcess(cmd, 0, stdout=dslr_output, stderr="")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda _: "/usr/bin/uv")
        result = prune_dslr_snapshots(keep=1)

        assert result == ["20260401_development-acme", "20260320_development-acme"]
        assert deleted == ["20260401_development-acme", "20260320_development-acme"]

    def test_keeps_n_snapshots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dslr_output = "20260403_dev-a  10MB\n20260402_dev-a  10MB\n20260401_dev-a  10MB\n"
        deleted: list[str] = []

        def fake_run(cmd, **kw):
            if "delete" in cmd:
                deleted.append(cmd[-1])
            return CompletedProcess(cmd, 0, stdout=dslr_output, stderr="")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda _: "/usr/bin/uv")
        result = prune_dslr_snapshots(keep=2)

        assert result == ["20260401_dev-a"]

    def test_returns_empty_when_no_dslr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda _: None)
        monkeypatch.delenv("DSLR_CMD", raising=False)
        assert prune_dslr_snapshots() == []

    def test_returns_empty_when_dslr_list_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_mod.subprocess, "run", lambda *a, **kw: CompletedProcess(a, 1, stdout="", stderr=""))
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda _: "/usr/bin/uv")
        assert prune_dslr_snapshots() == []


class TestPruneDslrSnapshotsSkipsInUseTenants:
    """A snapshot whose tenant is referenced by an in-flight worktree must not be pruned.

    Pre-fix the pruner ran unconditionally and globally: a worktree
    midway through provision (state=CREATED, DB not yet imported) lost
    its tenant's only snapshot, leaving no way to provision until a
    fresh remote dump was re-fetched. The guard now skips any tenant
    listed in *in_use_tenants*; the rest still prune normally.
    """

    def test_skips_pruning_for_in_use_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dslr_output = (
            "20260402_development-tenant-a  125MB\n"
            "20260401_development-tenant-a  123MB\n"
            "20260402_development-tenant-b  120MB\n"
            "20260401_development-tenant-b  118MB\n"
        )
        deleted: list[str] = []

        def fake_run(cmd, **kw):
            if "delete" in cmd:
                deleted.append(cmd[-1])
            return CompletedProcess(cmd, 0, stdout=dslr_output, stderr="")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda _: "/usr/bin/uv")
        # development-tenant-a is in use — none of its snapshots may be deleted.
        result = prune_dslr_snapshots(keep=1, in_use_tenants={"development-tenant-a"})

        assert result == ["20260401_development-tenant-b"]
        assert deleted == ["20260401_development-tenant-b"]

    def test_no_in_use_tenants_preserves_original_behavior(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dslr_output = "20260402_dev-a  10MB\n20260401_dev-a  10MB\n"
        deleted: list[str] = []

        def fake_run(cmd, **kw):
            if "delete" in cmd:
                deleted.append(cmd[-1])
            return CompletedProcess(cmd, 0, stdout=dslr_output, stderr="")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(dslr_mod.shutil, "which", lambda _: "/usr/bin/uv")
        # Empty in_use_tenants = same as the legacy unconditional prune.
        result = prune_dslr_snapshots(keep=1, in_use_tenants=set())

        assert result == ["20260401_dev-a"]
