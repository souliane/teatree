# test-path: cross-cutting — drives hooks/scripts/unapprovable_author_create_gate.py + teatree.cli.teatree_gate.
"""A raw MR/PR create on a distinct-author repo is refused before the MR exists.

A repo may declare that its MRs are authored under a non-owner credential so the human owner
stays eligible to approve them. A raw ``glab mr create`` / ``gh pr create`` never consults that
declaration — it writes under whatever credential the forge CLI carries, the owner's — and a
forge bars an author from approving their own MR, answering with HTTP 401 rather than 403. The
MR is born unapprovable and nothing said so until approval time; three had to be closed and
re-created in one day.

Deliberately narrower than the sibling out-of-band-merge gate's scope: a target that DECLARES a
distinct author BLOCKS, a target that resolves (managed or not) and declares NONE ALLOWS —
including a teatree-MANAGED repo with no such declaration (this repo's own upstream chief among
them, a review finding) — and only an unresolvable target falls back to the cwd, failing
safe to BLOCK.
"""

import json
import os
import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from typing import override
from unittest.mock import MagicMock, patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.unapprovable_author_create_gate as gate
from teatree.cli.teatree_gate import RAW_PR_CREATE_GATE_KEY
from teatree.core.overlay import OverlayBase, OverlayConfig

_IMPORT_ERROR = ImportError("teatree is not importable from this venue")


def _raise_import_error() -> None:
    raise _IMPORT_ERROR


class _BotAuthoredConfig(OverlayConfig):
    """Declares ``example-org/private-repo`` written under a resolvable non-owner credential."""

    def get_gitlab_token(self) -> str:
        return "owner-token"

    @override
    def get_gitlab_token_for_remote(self, remote: str) -> str:
        return "bot-token" if "private-repo" in remote else "owner-token"


def _declaring_bot_authored_repo() -> AbstractContextManager[MagicMock]:
    """Patch the registry so ``example-org/private-repo`` declares a non-owner author.

    Mirrors ``tests/teatree_core/test_ensure_pr.py``'s pattern: ``declared_distinct_author``
    reads ``get_all_overlays()`` directly, so a fake registered overlay is the isolated way to
    make one remote declare a credential without depending on this deployment's real one.
    """
    declaring = MagicMock(spec=OverlayBase)
    declaring.config = _BotAuthoredConfig()
    return patch("teatree.core.authoring_credential.get_all_overlays", return_value={"example": declaring})


class _UnreachableBotConfig(_BotAuthoredConfig):
    """Routes ``example-org/private-repo`` to a scoped credential that resolves to nothing here."""

    @override
    def get_gitlab_token_for_remote(self, remote: str) -> str:
        return "" if "private-repo" in remote else "owner-token"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607 — `git` from PATH deliberately: the fixture must build its remotes with the same git the gate under test resolves
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", remote_url)
    return path


def _event(command: str, cwd: Path | None) -> dict:
    return {
        "session_id": "sess-create",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd) if cwd is not None else "",
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestBlocksDistinctAuthorRepoCreate:
    @pytest.mark.parametrize(
        "command",
        [
            "glab mr create --title 'x' --description 'y'",
            "gh pr create --repo example-org/private-repo --title x --body y",
            "gh api repos/example-org/private-repo/pulls -f title=x -f head=a -f base=main",
            "glab api projects/example-org%2Fprivate-repo/merge_requests -X POST -f title=x",
        ],
    )
    def test_a_raw_create_aimed_at_a_distinct_author_repo_is_denied(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        with _declaring_bot_authored_repo():
            assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "pr create" in deny["permissionDecisionReason"]

    def test_the_refusal_names_the_ticketless_path_too(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A refusal naming only the ticket-bound path reads as a dead end for a branch with no
        # ticket, and a dead end is what sends the next agent back to the raw command.
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        with _declaring_bot_authored_repo():
            assert gate.handle_block_unapprovable_author_create(_event("glab mr create --title x", repo)) is True
        reason = (_parse_deny(capsys) or {})["permissionDecisionReason"]
        assert "ensure-pr" in reason
        assert "raw-pr-create disable" in reason

    @pytest.mark.parametrize(
        "command",
        ["glab mr create --title x", "gh pr create --repo example-org/private-repo --title x --body y"],
    )
    def test_an_unreachable_scoped_credential_still_blocks(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        declaring = MagicMock(spec=OverlayBase)
        declaring.config = _UnreachableBotConfig()
        with patch("teatree.core.authoring_credential.get_all_overlays", return_value={"example": declaring}):
            assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is True
        assert _parse_deny(capsys) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/9/merge_requests -f title=x",
            'gh pr create --repo "$REPO" --title x --body y',
            "gh api repos/$OWNER/$NAME/pulls -f title=x",
        ],
    )
    def test_an_opaque_or_dynamic_target_is_judged_by_the_cwd(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        with _declaring_bot_authored_repo():
            assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is True
        assert _parse_deny(capsys) is not None

    @pytest.mark.parametrize(
        ("command", "remote"),
        [
            (
                (
                    "gh pr create --repo example-org/public-repo --title a; "
                    "gh pr create --repo example-org/private-repo --title b"
                ),
                "git@github.com:example-org/public-repo.git",
            ),
            (
                "echo '--repo example-org/public-repo' && glab mr create --title x",
                "git@gitlab.com:example-org/private-repo.git",
            ),
        ],
    )
    def test_a_target_elsewhere_in_the_command_never_covers_another_create(
        self, command: str, remote: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", remote)
        with _declaring_bot_authored_repo():
            assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is True
        assert _parse_deny(capsys) is not None

    def test_the_registered_router_handler_is_this_gate(self) -> None:
        assert gate.handle_block_unapprovable_author_create in router._HANDLERS["PreToolUse"]


class TestAllowsUnmanagedRepoCreate:
    @pytest.mark.parametrize(
        "command",
        [
            "glab mr create --title x",
            "gh pr create --title x --body y",
        ],
    )
    def test_a_repo_no_overlay_claims_keeps_its_raw_create(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""

    def test_an_opaque_target_from_an_undeclared_cwd_keeps_its_raw_create(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        command = "glab api projects/9/merge_requests -f title=x"
        assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""

    def test_an_explicitly_named_unmanaged_target_allows_regardless_of_cwd(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        command = "gh pr create --repo example-org/public-repo --title x --body y"
        assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr view 3",
            "glab mr list",
            "gh api repos/example-org/private-repo/pulls",
            "gh api repos/example-org/private-repo/pulls/42 -X PATCH -f title=x",
            "echo 'run glab mr create --title x'",
            "cat <<EOF\nglab mr create --title x\nEOF",
            "echo 'gh api repos/example-org/private-repo/pulls -f title=x'",
        ],
    )
    def test_a_non_create_command_passes_through(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""


class TestAllowsManagedRepoWithNoDeclaration:
    """A managed repo no overlay declares a distinct author for keeps its raw create.

    a review finding: the out-of-band-MERGE gate is right to block on repo MANAGED-ness,
    but this gate's defect is identity-specific, so managed-ness alone must not be enough —
    this deployment's own upstream (``souliane/teatree``) is exactly this shape: registered in
    ``workspace_repos``, yet authored by the owner regardless of which surface opens the MR.
    """

    def test_a_managed_repo_declaring_no_distinct_author_keeps_its_raw_create(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        declaring = MagicMock(spec=OverlayBase)
        declaring.config = OverlayConfig()  # the plain default: no per-remote credential override
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        command = "gh pr create --repo souliane/teatree --title x --body y"
        with patch("teatree.core.authoring_credential.get_all_overlays", return_value={"example": declaring}):
            assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""

    def test_the_same_repo_flagless_from_its_own_cwd_also_keeps_its_raw_create(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        declaring = MagicMock(spec=OverlayBase)
        declaring.config = OverlayConfig()
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        command = "gh pr create --title x --body y"
        with patch("teatree.core.authoring_credential.get_all_overlays", return_value={"example": declaring}):
            assert gate.handle_block_unapprovable_author_create(_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""


class TestFailsSafeOnUncertainty:
    def test_an_unresolvable_cwd_and_target_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert gate.handle_block_unapprovable_author_create(_event("glab mr create --title x", None)) is True
        assert _parse_deny(capsys) is not None

    def test_a_repo_without_a_remote_is_blocked(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = tmp_path / "wt"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        assert gate.handle_block_unapprovable_author_create(_event("glab mr create --title x", repo)) is True
        assert _parse_deny(capsys) is not None

    def test_a_broken_detector_import_still_classifies_the_command_as_a_create(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(gate, "teatree_src_on_path", _raise_import_error)
        assert gate.raw_create_shape_reason("ls -la") == gate._DENY_PREFIX
        assert gate.handle_block_unapprovable_author_create(_event("ls -la", None)) is True
        assert _parse_deny(capsys) is not None


class TestKillSwitch:
    def test_the_gate_denies_by_default_and_allows_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        event = _event("glab mr create --title x", repo)
        with _declaring_bot_authored_repo():
            assert gate.handle_block_unapprovable_author_create(event) is True
            capsys.readouterr()

            monkeypatch.setattr(
                router,
                "_teatree_bool_setting",
                lambda key, default=True: False if key == gate.GATE_SETTING else default,
            )
            assert gate.handle_block_unapprovable_author_create(event) is False
        assert capsys.readouterr().out.strip() == ""

    def test_the_kill_switch_key_is_the_one_the_cli_registers(self) -> None:
        assert gate.GATE_SETTING == RAW_PR_CREATE_GATE_KEY
