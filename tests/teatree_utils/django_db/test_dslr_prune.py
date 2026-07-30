"""DSLR snapshot retention — parse, select, and the in-use / no-tool guards.

``dslr`` is an external subprocess, so it is patched; the selection arithmetic
(keep-N-newest per tenant, skip in-use tenants) runs for real.
"""

import types

import pytest

from teatree.core.management.commands._workspace.helpers import prune_dslr_snapshots_skipping
from teatree.utils.django_db import dslr_prune
from teatree.utils.django_db.dslr_prune import parse_dslr_snapshots, stale_dslr_snapshots


class TestParseDslrSnapshots:
    def test_groups_names_by_tenant_newest_first(self) -> None:
        stdout = "20260101_alpha\n20260103_alpha\n20260102_beta\n\n   \n"
        parsed = parse_dslr_snapshots(stdout)
        assert parsed["alpha"] == ["20260103_alpha", "20260101_alpha"]
        assert parsed["beta"] == ["20260102_beta"]

    def test_a_token_without_a_tenant_suffix_is_ignored(self) -> None:
        assert parse_dslr_snapshots("nodateprefix\n") == {}


class TestStaleDslrSnapshots:
    def test_no_dslr_tool_selects_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With no resolvable dslr command the pass is a safe no-op, never a guess.
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: None)
        assert stale_dslr_snapshots() == []

    def test_selects_all_but_the_kept_newest_per_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: ["dslr"])
        stdout = "20260101_alpha\n20260103_alpha\n20260102_alpha\n"
        monkeypatch.setattr(
            dslr_prune, "run_allowed_to_fail", lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=stdout)
        )
        assert stale_dslr_snapshots(keep=1) == ["20260102_alpha", "20260101_alpha"]

    def test_an_in_use_tenant_is_never_selected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: ["dslr"])
        stdout = "20260101_alpha\n20260103_alpha\n"
        monkeypatch.setattr(
            dslr_prune, "run_allowed_to_fail", lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=stdout)
        )
        assert stale_dslr_snapshots(keep=1, in_use_tenants={"alpha"}) == []

    def test_a_failed_list_command_selects_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: ["dslr"])
        monkeypatch.setattr(
            dslr_prune, "run_allowed_to_fail", lambda *_a, **_k: types.SimpleNamespace(returncode=1, stdout="")
        )
        assert stale_dslr_snapshots() == []


class TestPruneDslrSnapshotsSkipping:
    def test_dry_run_with_no_tool_yields_no_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: None)
        assert prune_dslr_snapshots_skipping(keep=1, in_use_tenants=set(), dry_run=True) == []

    def test_a_live_run_with_no_tool_yields_no_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: None)
        assert prune_dslr_snapshots_skipping(keep=1, in_use_tenants=set(), dry_run=False) == []

    def test_dry_run_previews_the_selected_snapshots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: ["dslr"])
        stdout = "20260101_alpha\n20260103_alpha\n"
        monkeypatch.setattr(
            dslr_prune, "run_allowed_to_fail", lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=stdout)
        )
        labels = prune_dslr_snapshots_skipping(keep=1, in_use_tenants=set(), dry_run=True)
        assert any("20260101_alpha" in label for label in labels)

    def test_a_live_run_deletes_the_stale_snapshots_and_labels_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The non-dry-run path issues a delete per stale snapshot and reports it.
        monkeypatch.setattr(dslr_prune, "find_dslr_cmd", lambda *_a, **_k: ["dslr"])
        stdout = "20260101_alpha\n20260103_alpha\n"
        deletes: list[list[str]] = []

        def _record(cmd: list[str], **_k: object) -> types.SimpleNamespace:
            deletes.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=stdout)

        monkeypatch.setattr(dslr_prune, "run_allowed_to_fail", _record)
        labels = prune_dslr_snapshots_skipping(keep=1, in_use_tenants=set(), dry_run=False)
        assert labels == ["Pruned DSLR snapshot: 20260101_alpha"]
        assert ["dslr", "delete", "-y", "20260101_alpha"] in deletes
