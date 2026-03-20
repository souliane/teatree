"""Tests for teetree.core.resolve — worktree resolution from PWD, env var, or explicit ID."""

from pathlib import Path
from unittest.mock import patch

import pytest

from teetree.core.models import Ticket, Worktree
from teetree.core.resolve import (
    WorktreeNotFoundError,
    _find_env_worktree_from_cwd,
    _match_worktree_by_path,
    _parse_env_file,
    resolve_worktree,
)

# ── _parse_env_file ──────────────────────────────────────────────────


def test_parse_env_file_basic(tmp_path: Path) -> None:
    envfile = tmp_path / ".env"
    envfile.write_text("KEY=value\nOTHER=123\n", encoding="utf-8")

    result = _parse_env_file(envfile)

    assert result == {"KEY": "value", "OTHER": "123"}


def test_parse_env_file_skips_comments_and_empty_lines(tmp_path: Path) -> None:
    envfile = tmp_path / ".env"
    envfile.write_text("# comment\n\nKEY=value\n  # indented comment\n\n", encoding="utf-8")

    result = _parse_env_file(envfile)

    assert result == {"KEY": "value"}


def test_parse_env_file_strips_whitespace(tmp_path: Path) -> None:
    envfile = tmp_path / ".env"
    envfile.write_text("  KEY  =  value  \n", encoding="utf-8")

    result = _parse_env_file(envfile)

    assert result == {"KEY": "value"}


def test_parse_env_file_skips_lines_without_equals(tmp_path: Path) -> None:
    envfile = tmp_path / ".env"
    envfile.write_text("NOEQUALS\nKEY=value\n", encoding="utf-8")

    result = _parse_env_file(envfile)

    assert result == {"KEY": "value"}


def test_parse_env_file_value_with_equals(tmp_path: Path) -> None:
    """Values containing '=' should be preserved (partition splits on first '=')."""
    envfile = tmp_path / ".env"
    envfile.write_text("URL=http://host?a=b\n", encoding="utf-8")

    result = _parse_env_file(envfile)

    assert result == {"URL": "http://host?a=b"}


# ── _find_env_worktree_from_cwd ─────────────────────────────────────


def test_find_env_worktree_found_in_cwd(tmp_path: Path) -> None:
    envfile = tmp_path / ".env.worktree"
    envfile.write_text("TICKET_DIR=/some/path\n", encoding="utf-8")

    with patch("teetree.core.resolve.Path.cwd", return_value=tmp_path):
        result = _find_env_worktree_from_cwd()

    assert result == envfile


def test_find_env_worktree_found_in_parent(tmp_path: Path) -> None:
    envfile = tmp_path / ".env.worktree"
    envfile.write_text("TICKET_DIR=/some/path\n", encoding="utf-8")
    child = tmp_path / "sub" / "deep"
    child.mkdir(parents=True)

    with patch("teetree.core.resolve.Path.cwd", return_value=child):
        result = _find_env_worktree_from_cwd()

    assert result == envfile


def test_find_env_worktree_not_found(tmp_path: Path) -> None:
    # tmp_path has no .env.worktree anywhere in the hierarchy up to tmp root
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)

    with patch("teetree.core.resolve.Path.cwd", return_value=child):
        result = _find_env_worktree_from_cwd()

    assert result is None


# ── _match_worktree_by_path ──────────────────────────────────────────


@pytest.mark.django_db
def test_match_worktree_by_path_exact_match() -> None:
    ticket = Ticket.objects.create()
    wt = Worktree.objects.create(
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        extra={"worktree_path": "/workspace/ac-backend-42/backend"},
    )

    result = _match_worktree_by_path("/workspace/ac-backend-42/backend")

    assert result is not None
    assert result.pk == wt.pk


@pytest.mark.django_db
def test_match_worktree_by_path_prefix_match() -> None:
    ticket = Ticket.objects.create()
    wt = Worktree.objects.create(
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        extra={"worktree_path": "/workspace/ac-backend-42"},
    )

    result = _match_worktree_by_path("/workspace/ac-backend-42/backend/src")

    assert result is not None
    assert result.pk == wt.pk


@pytest.mark.django_db
def test_match_worktree_by_path_no_match() -> None:
    ticket = Ticket.objects.create()
    Worktree.objects.create(
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        extra={"worktree_path": "/workspace/ac-backend-42"},
    )

    result = _match_worktree_by_path("/totally/different/path")

    assert result is None


@pytest.mark.django_db
def test_match_worktree_by_path_skips_empty_extra() -> None:
    ticket = Ticket.objects.create()
    Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature", extra={})

    result = _match_worktree_by_path("/some/path")

    assert result is None


# ── resolve_worktree ─────────────────────────────────────────────────


@pytest.mark.django_db
def test_resolve_worktree_explicit_id() -> None:
    ticket = Ticket.objects.create()
    wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature")

    result = resolve_worktree(worktree_id=wt.pk)

    assert result.pk == wt.pk


@pytest.mark.django_db
def test_resolve_worktree_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = Ticket.objects.create()
    wt = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="feature")
    monkeypatch.setenv("WT_ID", str(wt.pk))

    result = resolve_worktree()

    assert result.pk == wt.pk


@pytest.mark.django_db
def test_resolve_worktree_from_env_worktree_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ticket = Ticket.objects.create()
    wt = Worktree.objects.create(
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        extra={"worktree_path": str(tmp_path / "ticket-dir")},
    )
    monkeypatch.delenv("WT_ID", raising=False)

    envfile = tmp_path / ".env.worktree"
    envfile.write_text(f"TICKET_DIR={tmp_path / 'ticket-dir'}\n", encoding="utf-8")

    with patch("teetree.core.resolve._find_env_worktree_from_cwd", return_value=envfile):
        result = resolve_worktree()

    assert result.pk == wt.pk


@pytest.mark.django_db
def test_resolve_worktree_from_cwd_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ticket = Ticket.objects.create()
    wt_path = str(tmp_path / "workspace" / "ac-backend-42")
    wt = Worktree.objects.create(
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        extra={"worktree_path": wt_path},
    )
    monkeypatch.delenv("WT_ID", raising=False)

    with (
        patch("teetree.core.resolve._find_env_worktree_from_cwd", return_value=None),
        patch("teetree.core.resolve.Path.cwd", return_value=Path(wt_path)),
    ):
        result = resolve_worktree()

    assert result.pk == wt.pk


@pytest.mark.django_db
def test_resolve_worktree_raises_when_nothing_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WT_ID", raising=False)

    with (
        patch("teetree.core.resolve._find_env_worktree_from_cwd", return_value=None),
        patch("teetree.core.resolve.Path.cwd", return_value=tmp_path),
        pytest.raises(WorktreeNotFoundError, match="Cannot auto-detect worktree"),
    ):
        resolve_worktree()


@pytest.mark.django_db
def test_resolve_worktree_env_var_ignored_when_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WT_ID=0 or non-digit should be ignored, falling through to PWD resolution."""
    monkeypatch.setenv("WT_ID", "0")

    with (
        patch("teetree.core.resolve._find_env_worktree_from_cwd", return_value=None),
        patch("teetree.core.resolve.Path.cwd", return_value=tmp_path),
        pytest.raises(WorktreeNotFoundError),
    ):
        resolve_worktree()


@pytest.mark.django_db
def test_resolve_worktree_env_var_non_digit_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WT_ID", "abc")

    with (
        patch("teetree.core.resolve._find_env_worktree_from_cwd", return_value=None),
        patch("teetree.core.resolve.Path.cwd", return_value=tmp_path),
        pytest.raises(WorktreeNotFoundError),
    ):
        resolve_worktree()


@pytest.mark.django_db
def test_resolve_worktree_env_file_without_ticket_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When .env.worktree exists but has no TICKET_DIR, fall through to PWD match."""
    monkeypatch.delenv("WT_ID", raising=False)

    envfile = tmp_path / ".env.worktree"
    envfile.write_text("SOME_OTHER_KEY=value\n", encoding="utf-8")

    with (
        patch("teetree.core.resolve._find_env_worktree_from_cwd", return_value=envfile),
        patch("teetree.core.resolve.Path.cwd", return_value=tmp_path),
        pytest.raises(WorktreeNotFoundError),
    ):
        resolve_worktree()
