"""Tests for teatree.utils.django_db — DSLR snapshot parsing and pruning.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from subprocess import CompletedProcess

import pytest

from teatree.utils import django_db_dslr as dslr_mod
from teatree.utils import run as run_mod
from teatree.utils.django_db import prune_dslr_snapshots
from teatree.utils.django_db_dslr import parse_dslr_snapshots as _parse_dslr_snapshots


class TestParseDslrSnapshots:
    def test_groups_by_tenant(self) -> None:
        stdout = (
            "20260402_development-acme  125MB\n"
            "20260401_development-acme  123MB\n"
            "20260315_development-volksbank  98MB\n"
            "20260320_development-volksbank  100MB\n"
        )
        result = _parse_dslr_snapshots(stdout)
        assert set(result) == {"development-acme", "development-volksbank"}
        assert result["development-acme"] == [
            "20260402_development-acme",
            "20260401_development-acme",
        ]
        assert result["development-volksbank"] == [
            "20260320_development-volksbank",
            "20260315_development-volksbank",
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
