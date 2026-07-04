# test-path: cross-cutting
"""End-to-end tests for the snapshot-baseline prek hook.

Drives the real hook ``main()`` against a real git repo under ``tmp_path`` with
genuinely-staged files, proving the block / pass / fail-open / never-lockout
paths on the actual ``git diff --cached`` surface. The attestation is the
per-ticket green + posted ``E2eMandatoryRun`` the mandatory-E2E gate consumes.

Class-based ``@pytest.mark.django_db`` (never standalone functions, per
souliane/teatree#98) so the methods can still take ``tmp_path`` / ``monkeypatch``.
"""

import subprocess
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from scripts.hooks.check_snapshot_baseline import main
from teatree.core.models import E2eMandatoryRun, Ticket, Worktree

_URL = "https://example.com/issues/1#note_1"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)  # noqa: S607 — git on PATH


def _init_repo(root: Path) -> Path:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _stage_baseline(repo: Path) -> None:
    baseline = repo / "e2e" / "__snapshots__" / "home-chromium.png"
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes(b"\x89PNG fake baseline bytes")
    _git(repo, "add", "e2e/__snapshots__/home-chromium.png")


def _register_worktree(ticket: Ticket, repo: Path) -> None:
    Worktree.objects.create(
        ticket=ticket,
        repo_path=repo.name,
        branch=f"{ticket.pk}-x",
        extra={"worktree_path": str(repo.resolve())},
    )


def _point_cwd_at(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    resolved = str(repo.resolve())
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PWD", resolved)
    monkeypatch.delenv("T3_ORIG_CWD", raising=False)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestSnapshotBaselineHook:
    def test_silent_when_no_baseline_staged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _init_repo(tmp_path)
        (root / "README.md").write_text("docs\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _point_cwd_at(monkeypatch, root)
        assert main() == 0

    def test_blocks_baseline_without_attestation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _init_repo(tmp_path)
        ticket = Ticket.objects.create(issue_url="https://example.com/i/20")
        _register_worktree(ticket, root)
        _stage_baseline(root)
        _point_cwd_at(monkeypatch, root)
        assert main() == 1
        out = capsys.readouterr().out
        assert "e2e/__snapshots__/home-chromium.png" in out
        assert "record-e2e-run" in out

    def test_passes_baseline_with_attestation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _init_repo(tmp_path)
        ticket = Ticket.objects.create(issue_url="https://example.com/i/21")
        _register_worktree(ticket, root)
        E2eMandatoryRun.record(
            ticket=ticket, head_sha="a" * 40, spec="e2e/home.spec.ts", result="green", posted_url=_URL
        )
        _stage_baseline(root)
        _point_cwd_at(monkeypatch, root)
        assert main() == 0

    def test_fails_open_when_no_ticket_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _init_repo(tmp_path)  # no Worktree row -> unresolvable ticket
        _stage_baseline(root)
        _point_cwd_at(monkeypatch, root)
        assert main() == 0

    def test_allow_marker_sanctions_without_attestation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _init_repo(tmp_path)
        ticket = Ticket.objects.create(issue_url="https://example.com/i/22")
        _register_worktree(ticket, root)
        _stage_baseline(root)
        _point_cwd_at(monkeypatch, root)
        monkeypatch.setenv("ALLOW_SNAPSHOT_BASELINE", "verified the hero image by hand")
        assert main() == 0

    def test_db_kill_switch_disables_gate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # `t3 config_setting set snapshot_baseline_gate_enabled false` is a DB write; the
        # hook must honour it via the canonical DB-first resolver. RED before the fix: the
        # old hook read the kill-switch from ~/.teatree.toml RAW, so the DB row was ignored
        # and the un-attested baseline still blocked (main() stayed 1).
        root = _init_repo(tmp_path)
        ticket = Ticket.objects.create(issue_url="https://example.com/i/23")
        _register_worktree(ticket, root)
        _stage_baseline(root)
        _point_cwd_at(monkeypatch, root)
        assert main() == 1  # enabled by default -> un-attested baseline blocks
        call_command("config_setting", "set", "snapshot_baseline_gate_enabled", "false", stdout=StringIO())
        assert main() == 0  # the DB kill-switch now actuates the hook
