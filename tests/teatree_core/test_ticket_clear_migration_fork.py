"""``ticket clear`` refuses a CLEAR that would fork the migration graph (#995).

A forked migration graph (two branches each adding a migration off the
same parent) reaches ``clear+merge`` with CI green, then breaks the
post-merge self-DB ``migrate`` with ``Conflicting migrations detected``.
This pre-flight catches it at CLEAR time, BEFORE :meth:`MergeClear.issue`,
so the gate never certifies a state that is not end-to-end mergeable.
Real ``git init`` under ``tmp_path`` exercises the actual ``merge-tree``
prediction and migration-graph parse — no mocks on the git layer.
"""

import subprocess
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _clear_migration_fork
from teatree.core.management.commands._clear_migration_fork import (
    MIGRATION_LEAF_CONFLICT_REASON,
    check_clear_migration_fork,
)
from teatree.core.migration_leaf_probe import MigrationLeafConflict
from teatree.core.models import Ticket, Worktree

_BASE_MIGRATION = (
    "from django.db import migrations\n\n\n"
    "class Migration(migrations.Migration):\n"
    "    initial = True\n"
    "    dependencies = []\n"
    "    operations = []\n"
)


def _child_migration(parent: str) -> str:
    return (
        "from django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        f'    dependencies = [("core", "{parent}")]\n'
        "    operations = []\n"
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_migration(repo: Path, name: str, body: str) -> None:
    migrations_dir = repo / "src" / "teatree" / "core" / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    init = migrations_dir / "__init__.py"
    if not init.exists():
        init.write_text("")
    (migrations_dir / f"{name}.py").write_text(body)


def _seed_remote(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(seed, "config", "user.name", "Tester")
    _write_migration(seed, "0001_initial", _BASE_MIGRATION)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "initial migration")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


def _advance_remote_migration(tmp_path: Path, bare: Path, *, name: str, parent: str) -> None:
    work = tmp_path / f"advance-{name}"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    _write_migration(work, name, _child_migration(parent))
    _git(work, "add", "-A")
    _git(work, "commit", "-m", f"remote: add {name}")
    _git(work, "push", "origin", "main")


def _clone_with_feature_migration(tmp_path: Path, bare: Path, *, name: str, parent: str) -> tuple[Path, str]:
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    _git(clone, "checkout", "-b", "feature-branch")
    _write_migration(clone, name, _child_migration(parent))
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", f"feature: add {name}")
    feature_sha = _git(clone, "rev-parse", "HEAD")
    _git(clone, "fetch", "origin")
    return clone, feature_sha


class _SafeReview:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return False


class _SafeOverlay:
    """Non-impacting overlay double — keeps the orthogonal #1967 E2E gate inert."""

    review = _SafeReview()


class TestTicketClearMigrationFork(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        monkeypatch.setattr("teatree.core.gates.e2e_mandatory_gate.get_overlay", lambda *_a, **_k: _SafeOverlay())

    def _attach_ticket(self, clone: Path, branch: str) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch=branch,
            extra={"worktree_path": str(clone)},
        )
        return ticket

    def test_refuses_clear_for_forked_migration_graph(self) -> None:
        """The #995 symptom: branch B forks 0001 after branch A's 0002 merged."""
        bare = _seed_remote(self.tmp_path)
        clone, feature_sha = _clone_with_feature_migration(
            self.tmp_path, bare, name="0002_branch_b", parent="0001_initial"
        )
        _advance_remote_migration(self.tmp_path, bare, name="0002_branch_a", parent="0001_initial")
        _git(clone, "fetch", "origin")
        ticket = self._attach_ticket(clone, "feature-branch")

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "995",
                "souliane/teatree",
                reviewed_sha=feature_sha,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        assert result.get("issued") is False
        error = str(result.get("error", ""))
        assert MIGRATION_LEAF_CONFLICT_REASON in error
        assert "0002_branch_a" in error
        assert "0002_branch_b" in error

    def test_allows_clear_for_linear_migration_graph(self) -> None:
        """A migration chained off the current leaf is linear — the CLEAR issues."""
        bare = _seed_remote(self.tmp_path)
        clone, feature_sha = _clone_with_feature_migration(
            self.tmp_path, bare, name="0002_linear", parent="0001_initial"
        )
        # Main does not move — the feature's 0002 is the sole leaf.
        ticket = self._attach_ticket(clone, "feature-branch")

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "994",
                "souliane/teatree",
                reviewed_sha=feature_sha,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        assert result.get("issued") is True, f"linear-migration CLEAR refused: {result}"

    def test_no_worktree_skips_migration_fork_check(self) -> None:
        """Without a worktree to verify against, the probe is skipped (do-not-block)."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "993",
                "souliane/teatree",
                reviewed_sha="c" * 40,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        error = str(result.get("error", ""))
        assert MIGRATION_LEAF_CONFLICT_REASON not in error


class TestCheckClearMigrationForkResolution(TestCase):
    """Direct-call coverage of the worktree-resolution + skip branches."""

    def test_none_ticket_skips(self) -> None:
        assert check_clear_migration_fork("a" * 40, None) is None

    def test_no_worktree_skips(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        assert check_clear_migration_fork("a" * 40, ticket) is None

    def test_worktree_without_repo_path_skips(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="", branch="feat/x", extra={})
        assert check_clear_migration_fork("a" * 40, ticket) is None

    def test_prefers_ship_invoking_branch_worktree(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.IN_REVIEW,
            extra={"ship_invoking_branch": "feat/invoking", "target_branch": "develop"},
        )
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="/other", branch="feat/old", extra={"worktree_path": "/other"}
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/invoking",
            branch="feat/invoking",
            extra={"worktree_path": "/invoking"},
        )
        seen: dict[str, str] = {}

        def _fake_probe(repo: str, reviewed_sha: str, target: str) -> None:
            seen["repo"] = repo
            seen["target"] = target

        with mock.patch.object(_clear_migration_fork, "sha_forks_migration_graph", _fake_probe):
            assert check_clear_migration_fork("a" * 40, ticket) is None
        assert seen["repo"] == "/invoking"
        # An explicit target_branch with no slash is normalized to origin/<branch>.
        assert seen["target"] == "origin/develop"

    def test_renders_actionable_reason_on_fork(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="/repo", branch="feat/x", extra={"worktree_path": "/repo"}
        )
        conflict = MigrationLeafConflict(app_label="core", leaf_count=2, leaf_names=("0002_a", "0002_b"))
        with mock.patch.object(_clear_migration_fork, "sha_forks_migration_graph", return_value=conflict):
            reason = check_clear_migration_fork("deadbeef" + "0" * 32, ticket)
        assert reason is not None
        assert MIGRATION_LEAF_CONFLICT_REASON in reason
        assert "0002_a" in reason
        assert "0002_b" in reason
        assert "makemigrations --merge" in reason
