# test-path: cross-cutting — tests the main_clone_guard PreToolUse handler wired into hook_router.py.
"""Tests for the main-clone working-tree protection gate (#2836).

The incident: a sub-agent ran ``git checkout <feature-branch>`` inside the
teatree MAIN CLONE (the editable install), leaving it detached, dirty, and
behind ``origin/main`` — so ``t3`` ran stale code and the housekeeping
self-update could not fast-forward the dirty/detached tree.

The gate DENIES a working-tree mutation of a registered (teatree-managed) main
clone — an Edit/Write under it, or ``git checkout``/``switch`` off-default /
``reset --hard`` / ``restore`` / ``stash pop`` in a main-clone cwd — while
ALLOWING the same ops inside a worktree, ``git checkout <default>`` / ``git pull
--ff-only`` / ``git worktree add`` in the clone, and all read-only git, so ``t3
update`` and worktree creation keep working.

Real ``git`` under ``tmp_path``: a primary clone (``.git`` *dir*) with a
``souliane/teatree`` remote (so ``slug_for_cwd`` resolves it as managed offline)
and a linked worktree (``.git`` *file*). The DENY tests are anti-vacuous — they
go RED if the classifier stops finding the mutation (the gate is removed).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router

_MANAGED_REMOTE = "git@github.com:souliane/teatree.git"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _managed_main_clone(path: Path) -> Path:
    """A primary clone (``.git`` dir) with a managed remote and one commit on main."""
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", _MANAGED_REMOTE)
    (path / "app.py").write_text("x = 1\n")
    _git(path, "add", "app.py")
    _git(path, "commit", "-m", "init")
    return path


def _linked_worktree(clone: Path, wt: Path) -> Path:
    """A linked worktree (``.git`` file) branched off the clone."""
    _git(clone, "worktree", "add", "-b", "feat-x", str(wt))
    return wt


def _edit_event(file_path: Path, session: str) -> dict:
    return {
        "session_id": session,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(file_path), "old_string": "x = 1", "new_string": "x = 2"},
        "cwd": str(file_path.parent),
    }


def _bash_event(command: str, cwd: Path, session: str) -> dict:
    return {
        "session_id": session,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }


def _deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestDeniesMainCloneMutation:
    def test_edit_under_main_clone_is_denied(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        event = _edit_event(clone / "app.py", "sess-edit-deny")
        assert router.handle_block_main_clone_mutation(event) is True
        deny = _deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert "MAIN CLONE" in deny["permissionDecisionReason"]

    def test_git_checkout_feature_in_main_clone_cwd_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        event = _bash_event("git checkout feature", clone, "sess-co-deny")
        assert router.handle_block_main_clone_mutation(event) is True
        deny = _deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert "worktree" in deny["permissionDecisionReason"]

    @pytest.mark.parametrize(
        ("command", "session"),
        [
            ("git reset --hard origin/main", "sess-reset"),
            ("git restore app.py", "sess-restore"),
            ("git stash pop", "sess-stash"),
        ],
    )
    def test_dangerous_git_ops_in_main_clone_are_denied(
        self, command: str, session: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, session)) is True
        assert _deny(capsys) is not None


class TestAllowsWorktreeAndHygiene:
    def test_edit_inside_a_worktree_is_allowed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _edit_event(wt / "app.py", "sess-wt-edit")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    def test_edit_in_main_clone_on_a_feature_branch_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A bootstrap workflow that accumulates on one branch IN the main clone
        # is sanctioned (#3256): editing a non-default feature branch there is
        # the intended operation, not the pristine-default dirtying #2836 guards.
        clone = _managed_main_clone(tmp_path / "teatree")
        _git(clone, "checkout", "-b", "feat-bootstrap")
        event = _edit_event(clone / "app.py", "sess-feat-edit")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    def test_edit_in_main_clone_on_default_branch_is_still_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The true-positive protection is intact: an edit to the pristine default
        # checkout still blocks (the anti-vacuous companion to the feature-branch allow).
        clone = _managed_main_clone(tmp_path / "teatree")
        event = _edit_event(clone / "app.py", "sess-default-edit")
        assert router.handle_block_main_clone_mutation(event) is True
        deny = _deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_git_checkout_feature_inside_a_worktree_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _bash_event("git checkout other-feature", wt, "sess-wt-co")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    @pytest.mark.parametrize(
        ("command", "session"),
        [
            ("git checkout main", "sess-co-main"),
            ("git pull --ff-only", "sess-pull"),
            ("git fetch origin", "sess-fetch"),
            ("git worktree add -b feat ../wt2 origin/main", "sess-wt-add"),
            ("git status", "sess-status"),
            ("git log --oneline", "sess-log"),
        ],
    )
    def test_hygiene_and_read_only_git_in_main_clone_is_allowed(
        self, command: str, session: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, session)) is False
        assert _deny(capsys) is None

    def test_unmanaged_main_clone_is_not_gated(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # A repo no overlay owns (a random clone) must never be blocked.
        clone = tmp_path / "random"
        clone.mkdir()
        _git(clone, "init", "-b", "main")
        _git(clone, "remote", "add", "origin", "git@github.com:randomuser/randomrepo.git")
        (clone / "app.py").write_text("x = 1\n")
        _git(clone, "add", "app.py")
        _git(clone, "commit", "-m", "init")
        event = _bash_event("git checkout feature", clone, "sess-unmanaged")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None


class TestTUpdateNotBlocked:
    """``t3 update`` runs fetch + ff-pull from the main clone cwd — never gated."""

    @pytest.mark.parametrize(
        ("command", "session"),
        [
            ("git fetch origin", "sess-tu-fetch"),
            ("git pull --ff-only", "sess-tu-pull"),
            ("git -C . pull --ff-only", "sess-tu-pull-c"),
        ],
    )
    def test_t3_update_git_ops_are_allowed(
        self, command: str, session: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, session)) is False
        assert _deny(capsys) is None


class TestRedirectionKeysOffEffectiveRepo:
    """``-C`` / ``--git-dir`` redirection keys the gate off the TARGETED repo (#2844 #2).

    The gate must classify against the repo the command MUTATES, not the ambient
    cwd: ``git -C <main-clone>`` from a worktree cwd mutates the clone (DENY),
    and ``git -C <worktree>`` from a clone cwd mutates the worktree (ALLOW).
    """

    def test_dash_c_into_main_clone_from_worktree_cwd_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The bypass: cwd is a worktree, but ``-C <main-clone>`` redirects the
        # mutation INTO the main clone. Must block despite the benign cwd.
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _bash_event(f"git -C {clone} checkout feature", wt, "sess-c-bypass")
        assert router.handle_block_main_clone_mutation(event) is True
        deny = _deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_cd_then_dash_c_into_main_clone_is_denied(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # ``cd <worktree> && git -C <main-clone> checkout feature`` — both the
        # leading cd and git's -C are honoured; the absolute -C wins → clone.
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _bash_event(f"cd {wt} && git -C {clone} checkout feature", wt, "sess-cd-c")
        assert router.handle_block_main_clone_mutation(event) is True
        assert _deny(capsys) is not None

    def test_git_dir_into_main_clone_from_worktree_cwd_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ``--git-dir <main-clone>/.git`` targets the clone's refs; normalised to
        # the enclosing clone root, the off-default checkout must block.
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _bash_event(f"git --git-dir {clone}/.git checkout feature", wt, "sess-gitdir")
        assert router.handle_block_main_clone_mutation(event) is True
        assert _deny(capsys) is not None

    def test_dash_c_into_worktree_from_main_clone_cwd_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The mirror false-positive: cwd is the main clone, but ``-C <worktree>``
        # redirects the mutation INTO the worktree — a legitimate op. Must allow.
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _bash_event(f"git -C {wt} checkout -b another", clone, "sess-c-mirror")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    def test_git_dir_env_form_is_a_documented_cwd_keying_limitation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # LIMITATION (pinned, not silent): the bare ``GIT_DIR=<clone>/.git git …``
        # ENV-variable redirection form is not parsed — only the ``-C`` /
        # ``--git-dir`` ARG forms are — so from a non-clone cwd it falls back to
        # cwd-keying and is NOT caught. The common ``-C`` form (tested above) is.
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _bash_event(f"GIT_DIR={clone}/.git git checkout feature", wt, "sess-env")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None


class TestNonStandardDefaultBranch:
    """A clone whose real default is ``develop`` (no ``origin/HEAD``) is not over-blocked (#2844 #4)."""

    def _develop_clone_without_origin_head(self, path: Path) -> Path:
        # A managed clone sitting on ``develop`` with NO origin/HEAD pointer —
        # the real default is neither origin/HEAD-resolvable nor in {main,master}.
        path.mkdir(parents=True)
        _git(path, "init", "-b", "develop")
        _git(path, "remote", "add", "origin", _MANAGED_REMOTE)
        (path / "app.py").write_text("x = 1\n")
        _git(path, "add", "app.py")
        _git(path, "commit", "-m", "init")
        return path

    def test_checkout_of_real_default_develop_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = self._develop_clone_without_origin_head(tmp_path / "teatree")
        event = _bash_event("git checkout develop", clone, "sess-develop-ok")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    def test_checkout_of_other_branch_still_blocked_on_develop_clone(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The fallback adds ONLY the current branch (develop) to the safe set —
        # an off-default switch is still blocked, so the gate is not weakened.
        clone = self._develop_clone_without_origin_head(tmp_path / "teatree")
        event = _bash_event("git checkout feature", clone, "sess-develop-feat")
        assert router.handle_block_main_clone_mutation(event) is True
        assert _deny(capsys) is not None


class TestBashMediatedWriteIntoMainClone:
    """A file WRITTEN through Bash into the clone is a mutation too (#4092).

    Before this arm the guard classified a tool call two ways — an Edit/Write
    ``file_path``, or a git subcommand — and a shell write (``sed -i``,
    ``cat > path <<EOF``, a ``python3`` heredoc) matched neither, so it was
    allowed. It then fired on the git command used to REVERT, i.e. after the
    shared base was already dirty. These tests pin the write-time deny.
    """

    def test_sed_in_place_on_a_main_clone_file_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        event = _bash_event(f"sed -i 's/x = 1/x = 2/' {clone}/app.py", clone, "sess-sed-deny")
        assert router.handle_block_main_clone_mutation(event) is True
        deny = _deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert "MAIN CLONE" in deny["permissionDecisionReason"]

    def test_heredoc_redirect_into_a_main_clone_file_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        command = f"cat > {clone}/app.py <<'EOF'\nx = 2\nEOF\n"
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, "sess-heredoc-deny")) is True
        assert _deny(capsys) is not None

    def test_python_heredoc_write_into_a_main_clone_file_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        command = f'python3 - <<PY\nopen("{clone}/app.py", "w").write("x = 2\\n")\nPY\n'
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, "sess-py-deny")) is True
        assert _deny(capsys) is not None

    def test_relative_write_from_a_main_clone_cwd_is_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The recorded incident shape: the cwd DRIFTED into the clone, so the
        # write target was a bare relative path with nothing naming the clone.
        clone = _managed_main_clone(tmp_path / "teatree")
        assert router.handle_block_main_clone_mutation(_bash_event("sed -i 's/1/2/' app.py", clone, "sess-rel")) is True
        assert _deny(capsys) is not None

    def test_write_into_a_worktree_is_allowed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        wt = _linked_worktree(clone, tmp_path / "wt")
        event = _bash_event(f"sed -i 's/x = 1/x = 2/' {wt}/app.py", wt, "sess-wt-sed")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    def test_write_to_a_scratch_path_from_a_main_clone_cwd_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The false positive that would make this gate unusable: a heredoc into
        # /tmp (or any path outside the clone) run from a main-clone cwd.
        clone = _managed_main_clone(tmp_path / "teatree")
        command = f"cat > {tmp_path}/scratch.txt <<'EOF'\nnotes\nEOF\n"
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, "sess-scratch")) is False
        assert _deny(capsys) is None

    def test_unresolvable_write_target_is_allowed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # This module's posture on anything it cannot pin statically is ALLOW —
        # the same stance it already takes on an unpinnable git target.
        clone = _managed_main_clone(tmp_path / "teatree")
        event = _bash_event('echo hi > "$OUT/app.py"', clone, "sess-unresolvable")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    def test_read_only_command_on_a_main_clone_file_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        event = _bash_event(f"grep -n 'x' {clone}/app.py && cat {clone}/app.py", clone, "sess-readonly")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    @pytest.mark.parametrize(
        ("command", "session"),
        [
            ("grep -rn '>' .", "sess-quoted-gt"),
            ('git commit -m "> blockquote note"', "sess-quoted-blockquote"),
            ("rg -n '>>' app.py", "sess-quoted-append"),
        ],
    )
    def test_a_quoted_redirect_character_is_an_argument_not_a_write(
        self, command: str, session: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Redirection is shell SYNTAX. Classifying it on the quote-DECODED token
        # read `grep -rn '>' .` as writing the clone root and denied it.
        clone = _managed_main_clone(tmp_path / "teatree")
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, session)) is False
        assert _deny(capsys) is None

    @pytest.mark.parametrize(
        ("command", "session"),
        [
            ("cat in.txt | tee >(gzip -c > /tmp/a.gz)", "sess-procsub-tee"),
            ("make 2> >(tee /tmp/err.log)", "sess-procsub-stderr"),
        ],
    )
    def test_process_substitution_is_not_a_write_into_the_clone(
        self, command: str, session: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `>(...)` was parsed as a `> path` redirect, so the guard denied a
        # legitimate command while naming `<clone>/(gzip` — a path that does not
        # exist, which reads as a broken guard rather than a rule (#4127).
        clone = _managed_main_clone(tmp_path / "teatree")
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, session)) is False
        assert _deny(capsys) is None

    def test_a_real_redirect_beside_a_substitution_is_still_denied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The anti-vacuous companion: reporting the substitution unresolved must
        # not blind the guard to a genuine `> path` on the same line (#4127).
        clone = _managed_main_clone(tmp_path / "teatree")
        command = f"make 2> >(tee /tmp/err.log) > {clone}/app.py"
        assert router.handle_block_main_clone_mutation(_bash_event(command, clone, "sess-procsub-real")) is True
        assert _deny(capsys) is not None

    def test_write_into_a_main_clone_on_a_feature_branch_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same #3256 carve-out the Edit/Write arm already honours.
        clone = _managed_main_clone(tmp_path / "teatree")
        _git(clone, "checkout", "-b", "feat-bootstrap")
        event = _bash_event(f"sed -i 's/x = 1/x = 2/' {clone}/app.py", clone, "sess-feat-sed")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None


class TestNeverLockout:
    def test_per_call_token_allows(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        event = _bash_event("git checkout feature  [main-clone-ok: rescue]", clone, "sess-token")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None

    def test_kill_switch_disables_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clone = _managed_main_clone(tmp_path / "teatree")
        monkeypatch.setattr(
            router,
            "_teatree_bool_setting",
            lambda key, default=True: False if key == "main_clone_guard_gate_enabled" else default,
        )
        event = _bash_event("git checkout feature", clone, "sess-killswitch")
        assert router.handle_block_main_clone_mutation(event) is False
        assert _deny(capsys) is None
