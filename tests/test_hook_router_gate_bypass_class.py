"""RED-first tests for the gate-bypass class hardening (closes #1610).

Each finding has a RED section asserting the evasion command is currently
NOT blocked on main, then a GREEN section asserting it IS blocked after
the fix. The RED assertions are removed once the fix lands (the green tests
are the durable regression guard).

Findings covered: F1 (double-space substring), F2 (gh/glab api create
endpoint), F3 (core.hooksPath bypass), F4 (double-space glab api
review-post), F5 (missing glab mr update / gh pr comment), F6 (readonly-
prefix chain bypass), F7 (ls -lRa missed), F8 (pipeline auto-merge).
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _ORCHESTRATOR_HEAVY_BASH_RE,
    _deny_match,
    _extract_ai_sig_payload,
    _extract_mr_fields,
    _is_merge_class_mutation,
    _is_raw_review_write,
    handle_block_ai_signature,
    handle_block_direct_commands,
    handle_block_raw_review_post,
    handle_enforce_orchestrator_boundary,
    handle_validate_mr_metadata,
)

# ── helpers ─────────────────────────────────────────────────────────────


def _bash_event(command: str) -> dict:
    return {
        "session_id": "sess-bypass",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def _main_agent_bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


@pytest.fixture(autouse=True)
def _gate_enabled_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate orchestrator gate from developer's real ~/.teatree.toml."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


# ── F1: double-space substring bypass ───────────────────────────────────


class TestF1DoubleSpaceBypass:
    """F1 CRITICAL: double-space variants evade plain-`in` substring checks.

    Evasion: `git  commit` / `glab  mr  create` with double (or any extra)
    whitespace between tokens bypassed the `in` guard in `_extract_ai_sig_payload`
    and `_extract_mr_fields`, letting a Co-Authored-By trailer and a non-compliant
    MR title reach the forge unscanned.
    """

    # ── AI-sig gate (F1a) ────────────────────────────────────────────────

    def test_f1a_double_space_git_commit_triggers_ai_sig_scan(self, monkeypatch, capsys):
        """Git  commit (double space) must be treated as a git-commit for AI-sig scan."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git  commit -m 'fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>'"},
        }
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_block_ai_signature(data)
        assert blocked is True, "double-space git  commit must trigger AI-sig scan and be blocked"
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_f1a_triple_space_git_commit_triggers_ai_sig_scan(self, monkeypatch, capsys):
        """Git   commit (triple space) must also be caught."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        data = {"tool_name": "Bash", "tool_input": {"command": "git   commit -m 'bad trailer'"}}
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_block_ai_signature(data)
        assert blocked is True, "triple-space git   commit must trigger AI-sig scan"

    def test_f1a_single_space_git_commit_still_blocked(self, monkeypatch, capsys):
        """Existing single-space form must not regress."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        data = {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'Co-Authored-By: Claude'"}}
        with patch.object(router.subprocess, "run", return_value=rejected):
            assert handle_block_ai_signature(data) is True

    def test_f1a_extract_payload_double_space_git_commit(self):
        """_extract_ai_sig_payload returns the body for double-space form."""
        data = {"tool_name": "Bash", "tool_input": {"command": "git  commit -m 'the body'"}}
        payload = _extract_ai_sig_payload(data)
        assert payload == "the body", f"expected 'the body', got {payload!r}"

    def test_f1a_extract_payload_double_space_glab_mr_create(self):
        """_extract_ai_sig_payload triggers on glab  mr  create (double space)."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab  mr  create --title 't' --body 'the body'"},
        }
        payload = _extract_ai_sig_payload(data)
        assert payload is not None, "double-space glab  mr  create must be recognised"

    def test_f1a_extract_payload_double_space_gh_pr_create(self):
        """_extract_ai_sig_payload triggers on gh  pr  create (double space)."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh  pr  create --title 't' --body 'the body'"},
        }
        payload = _extract_ai_sig_payload(data)
        assert payload is not None, "double-space gh  pr  create must be recognised"

    # ── validate-MR gate (F1b) ───────────────────────────────────────────

    def test_f1b_double_space_glab_mr_create_triggers_mr_validation(self, monkeypatch, capsys):
        """Glab  mr  create (double space) must be caught by validate-MR gate."""
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab  mr  create --title 'fix: x (p#1)' --description 'desc'"},
        }
        with patch.object(router.subprocess, "run", return_value=ok):
            result = handle_validate_mr_metadata(data)
        # The validator ran (we patched it to return 0) — result is False (not denied).
        # Key: _extract_mr_fields must have returned a tuple (not None).
        assert result is False, "double-space glab  mr  create should call the validator and pass (rc=0)"

    def test_f1b_double_space_glab_mr_update_triggers_mr_validation(self, monkeypatch, capsys):
        """Glab  mr  update (double space) must be caught by validate-MR gate."""
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Bad title")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab  mr  update --title '' --description ''"},
        }
        with patch.object(router.subprocess, "run", return_value=rejected):
            result = handle_validate_mr_metadata(data)
        assert result is True, "double-space glab  mr  update with bad title must be denied"

    def test_f1b_extract_mr_fields_double_space_glab_mr_create(self):
        """_extract_mr_fields returns a tuple for double-space form."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab  mr  create --title 't' --description 'd'"},
        }
        fields = _extract_mr_fields(data)
        assert fields is not None, "double-space glab  mr  create must be recognised by _extract_mr_fields"

    def test_f1b_extract_mr_fields_double_space_glab_mr_update(self):
        """_extract_mr_fields returns a tuple for double-space glab  mr  update."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab  mr  update --title 't' --description 'd'"},
        }
        fields = _extract_mr_fields(data)
        assert fields is not None, "double-space glab  mr  update must be recognised by _extract_mr_fields"

    def test_f1b_single_space_glab_mr_create_still_caught(self, monkeypatch):
        """Existing single-space form must not regress."""
        monkeypatch.delenv("T3_MR_VALIDATE_SCRIPT", raising=False)
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr create --title 'fix: x (p#1)' --description 'desc'"},
        }
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_validate_mr_metadata(data) is False  # validator ran, rc=0


# ── F2: gh/glab api create endpoint bypass ───────────────────────────────


class TestF2ApiCreateEndpointBypass:
    """F2 HIGH: REST-API create-endpoint bypasses diff-coverage and AI-sig gates.

    `gh api repos/example-org/private-repo/pulls -X POST` and
    `glab api projects/42/merge_requests --method POST` create a PR/MR
    without going through `gh pr create` / `glab mr create`, so the
    `_PR_MR_CREATE_RE` and `pr_cmds` checks were never triggered.
    """

    # ── AI-sig gate (F2a) ────────────────────────────────────────────────

    def test_f2a_gh_api_pulls_post_triggers_ai_sig_scan(self, monkeypatch, capsys):
        """Gh api .../pulls POST must be treated as a PR create for AI-sig."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        cmd = "gh api repos/example-org/private-repo/pulls -X POST -f title='t' -f body='Generated with [Claude Code]'"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_block_ai_signature(data)
        assert blocked is True, "gh api .../pulls POST must trigger AI-sig scan"
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_f2a_glab_api_merge_requests_post_triggers_ai_sig_scan(self, monkeypatch, capsys):
        """Glab api .../merge_requests POST must be treated as an MR create for AI-sig."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        data = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "glab api projects/42/merge_requests --method POST -f title='t' -f description='bad trailer'"
            },
        }
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_block_ai_signature(data)
        assert blocked is True, "glab api .../merge_requests POST must trigger AI-sig scan"

    def test_f2a_gh_api_pulls_get_does_not_trigger_ai_sig(self):
        """Gh api .../pulls GET (read) must NOT trigger AI-sig (no deny)."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh api repos/example-org/private-repo/pulls -X GET"},
        }
        payload = _extract_ai_sig_payload(data)
        assert payload is None, "GET to pulls endpoint must not be treated as a PR create"

    def test_f2a_glab_api_merge_requests_get_does_not_trigger_ai_sig(self):
        """Glab api .../merge_requests GET (read) must NOT trigger AI-sig."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab api projects/42/merge_requests"},
        }
        payload = _extract_ai_sig_payload(data)
        assert payload is None, "bare GET to merge_requests must not be treated as a create"

    # ── diff-coverage gate / _is_merge_class_mutation (F2b) ─────────────

    def test_f2b_gh_api_pulls_post_is_merge_class_mutation(self):
        """Gh api .../pulls POST is a merge-class mutation (diff-coverage gate)."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh api repos/example-org/private-repo/pulls -X POST -f title='t'"},
        }
        assert _is_merge_class_mutation(data) is True, "gh api pulls POST must be merge-class"

    def test_f2b_glab_api_merge_requests_post_is_merge_class_mutation(self):
        """Glab api .../merge_requests POST is a merge-class mutation."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab api projects/42/merge_requests --method POST -f title='t'"},
        }
        assert _is_merge_class_mutation(data) is True, "glab api merge_requests POST must be merge-class"

    def test_f2b_gh_api_pulls_get_is_not_merge_class_mutation(self):
        """Gh api .../pulls GET is NOT a merge-class mutation."""
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh api repos/example-org/private-repo/pulls"},
        }
        assert _is_merge_class_mutation(data) is False, "bare GET to pulls must not be merge-class"


# ── F3: git -c core.hooksPath bypass ────────────────────────────────────


class TestF3CoreHooksPathBypass:
    """F3 HIGH: git -c core.hooksPath=/dev/null commit bypasses pre-commit hooks.

    `_BLOCKED_COMMANDS` blocked `--no-verify` but not the semantically
    equivalent `-c core.hooksPath=/dev/null` flag that points git's hooks
    directory at /dev/null, silencing all hooks.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git -c core.hooksPath=/dev/null commit -m 'bypass'",
            "git -c core.hooksPath= commit -m 'bypass'",
            "git commit -c core.hooksPath=/dev/null -m 'x'",
            "git -c core.hooksPath=/tmp commit -m 'bypass'",
            "git -c 'core.hooksPath=/dev/null' commit -m 'bypass'",
            'git -c "core.hooksPath=/dev/null" commit -m x',
        ],
    )
    def test_f3_hooks_path_override_is_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True, f"core.hooksPath bypass must be blocked: {command!r}"
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_f3_deny_message_mentions_no_verify(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_block_direct_commands(_bash_event("git -c core.hooksPath=/dev/null commit -m 'x'"))
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "hooksPath" in deny["permissionDecisionReason"] or "hook" in deny["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'normal commit'",
            "git -c user.name=Bot commit -m 'ok'",
            "git config --get core.hooksPath",
            "grep 'core.hooksPath' .git/config",
        ],
    )
    def test_f3_legitimate_git_config_not_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is not True, f"legitimate command must not be blocked: {command!r}"
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        "command",
        [
            # lowercase — git config keys are case-insensitive per git-config(1)
            "git -c core.hookspath=/dev/null commit --allow-empty -m x",
            "git commit -c core.hookspath=/dev/null -m x",
            # uppercase
            "git -c CORE.HOOKSPATH=/dev/null commit --allow-empty -m x",
            # mixed case
            "git -c core.HooksPath=/dev/null commit -m bypass",
            "git -c Core.hooksPath=/dev/null commit -m bypass",
        ],
    )
    def test_f3_case_insensitive_hooks_path_is_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Git config keys are case-insensitive; all case variants must be blocked."""
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True, f"case-variant core.hooksPath bypass must be blocked: {command!r}"
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"


# ── F4: double-space glab  api review-post bypass ────────────────────────


class TestF4DoubleSpaceReviewPostBypass:
    """F4 HIGH: glab  api (double space) bypasses the review-post deny gate.

    `_is_raw_review_write` checked `"glab api" not in command` (plain `in`),
    so `glab  api projects/42/merge_requests/7/discussions -X POST` slipped
    through the draft-default/dedup enforcement.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "glab  api projects/42/merge_requests/7/discussions -X POST -f body='hi'",
            "glab  api projects/42/merge_requests/7/notes --method POST -f body='nit'",
            "gh  api repos/o/r/pulls/12/comments -X POST -f body='please fix'",
            "glab   api projects/42/merge_requests/7/discussions -f body=hi",
        ],
    )
    def test_f4_double_space_api_review_write_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_raw_review_post(_bash_event(command)) is True, (
            f"double-space api review write must be denied: {command!r}"
        )
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            "glab  api projects/42/merge_requests/7/discussions",
            "gh  api repos/o/r/pulls/12/comments -X GET",
        ],
    )
    def test_f4_double_space_api_get_reads_still_pass(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        """GET reads with double-space must still pass (no false deny)."""
        result = handle_block_raw_review_post(_bash_event(command))
        assert result is not True, f"GET read must not be denied: {command!r}"
        assert capsys.readouterr().out.strip() == ""

    def test_f4_is_raw_review_write_double_space(self) -> None:
        assert _is_raw_review_write("glab  api projects/42/merge_requests/7/discussions -X POST -f body=hi") is True
        assert _is_raw_review_write("glab  api projects/42/merge_requests/7/discussions") is False


# ── F5: missing glab mr update / gh pr comment in AI-sig gate ─────────────


class TestF5MissingEditCommentInAiSig:
    """F5 MEDIUM: `glab mr update` and `gh pr comment` can carry AI signatures.

    The MR description edit-class command (`glab mr update`) and a PR comment
    both carry a body the AI-signature gate must scan. (`glab mr edit` is NOT a
    real glab subcommand — glab 1.80.x uses `update`; the previous hand-rolled
    regex defensively included a non-existent `edit` verb. The shared canonical
    parser tracks glab's real surface: `create` / `update` / `note`.)
    """

    def test_f5_glab_mr_update_triggers_ai_sig_scan(self, monkeypatch, capsys):
        """Glab mr update with a banned trailer in --description must be blocked."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr update 7 --description 'fix: x\n\nGenerated with [Claude Code]'"},
        }
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_block_ai_signature(data)
        assert blocked is True, "glab mr update with banned trailer must be blocked"

    def test_f5_gh_pr_comment_triggers_ai_sig_scan(self, monkeypatch, capsys):
        """Gh pr comment with a banned trailer must be blocked."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        rejected = subprocess.CompletedProcess(args=[], returncode=1, stdout="banned", stderr="")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr comment 12 --body 'see fix\n\nGenerated with [Claude Code]'"},
        }
        with patch.object(router.subprocess, "run", return_value=rejected):
            blocked = handle_block_ai_signature(data)
        assert blocked is True, "gh pr comment with banned trailer must be blocked"

    def test_f5_glab_mr_update_payload_extracted(self):
        """_extract_ai_sig_payload returns a payload for glab mr update."""
        data = {"tool_name": "Bash", "tool_input": {"command": "glab mr update 7 --description 'the body'"}}
        payload = _extract_ai_sig_payload(data)
        assert payload is not None, "glab mr update must be recognised by _extract_ai_sig_payload"

    def test_f5_gh_pr_comment_payload_extracted(self):
        """_extract_ai_sig_payload returns a payload for gh pr comment."""
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr comment 12 --body 'the body'"}}
        payload = _extract_ai_sig_payload(data)
        assert payload is not None, "gh pr comment must be recognised by _extract_ai_sig_payload"

    def test_f5_clean_glab_mr_update_allowed(self, monkeypatch):
        """Glab mr update with a clean body must pass."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="clean", stderr="")
        data = {"tool_name": "Bash", "tool_input": {"command": "glab mr update 7 --description 'clean body'"}}
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_block_ai_signature(data) is False

    def test_f5_clean_gh_pr_comment_allowed(self, monkeypatch):
        """Gh pr comment with a clean body must pass."""
        monkeypatch.setattr(router.shutil, "which", lambda _: "/usr/local/bin/t3")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="clean", stderr="")
        data = {"tool_name": "Bash", "tool_input": {"command": "gh pr comment 12 --body 'clean note'"}}
        with patch.object(router.subprocess, "run", return_value=ok):
            assert handle_block_ai_signature(data) is False


# ── F6: readonly-prefix chain bypass ────────────────────────────────────


class TestF6ReadonlyPrefixChainBypass:
    """F6 MEDIUM: readonly-prefix short-circuit bypasses blocked commands in chains.

    `_deny_match` returned None (allow) when the command started with a
    readonly prefix (grep, cat, etc.), even when the rest of the command
    contained a blocked sub-command via `;`, `&&`, `||`, `|`, `$(` or
    backtick chaining.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "grep '' /dev/null; python manage.py runserver",
            "cat README.md && npm run build",
            "echo hi || npm run serve",
            "cat README.md | npm run serve",
            "grep foo bar; pip install requests",
            "cat file.txt; pg_dump mydb > dump.sql",
            "echo x; createdb newdb",
            "grep pat file; dslr restore my_snap",
        ],
    )
    def test_f6_chained_blocked_command_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True, f"chained blocked command must be denied: {command!r}"
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            "grep 'manage.py runserver' README.md",
            "cat README.md",
            "echo 'npm run serve is blocked'",
            "grep playwright tests/",
            "rg 'pip install' .",
        ],
    )
    def test_f6_pure_readonly_prefix_without_chain_still_allowed(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pure readonly commands (no chain operator) must not be falsely denied."""
        result = handle_block_direct_commands(_bash_event(command))
        assert result is not True, f"pure readonly command must not be denied: {command!r}"
        assert capsys.readouterr().out.strip() == ""

    def test_f6_deny_match_semicolon_chain(self) -> None:
        reason = _deny_match("grep '' /dev/null; python manage.py runserver")
        assert reason is not None, "semicolon-chained blocked command must return a deny reason"

    def test_f6_deny_match_and_chain(self) -> None:
        reason = _deny_match("cat file && npm run build")
        assert reason is not None, "&&-chained blocked command must return a deny reason"

    def test_f6_deny_match_pure_readonly_no_chain(self) -> None:
        reason = _deny_match("grep 'manage.py runserver' README.md")
        assert reason is None, "pure readonly grep mention must be allowed"

    # ── F6 over-block: blocked tool-names inside quoted args false-block ──

    @pytest.mark.parametrize(
        "command",
        [
            'git commit -m "fix: handle pip install edge case"',
            "git commit -m 'fix: handle pip install edge case'",
            'git log --oneline | grep "pip install"',
            'cat setup.py | grep "manage.py migrate"',
            'echo "checking docker compose up" && ls',
            'git diff origin/main...HEAD | grep -E "def |pip install"',
        ],
    )
    def test_f6_quoted_tool_name_in_arg_must_allow(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Blocked tool names that appear only inside quoted args must not be denied."""
        result = handle_block_direct_commands(_bash_event(command))
        assert result is not True, f"quoted-arg tool name must not be denied: {command!r}"
        assert capsys.readouterr().out.strip() == ""


# ── F7: ls -lRa missed by orchestrator boundary ──────────────────────────


class TestF7LsRecursiveFlagNotAtWordBoundary:
    r"""F7 LOW: `ls -lRa` has R not at a word boundary — old pattern missed it.

    `_ORCHESTRATOR_HEAVY_BASH_RE` used `\bls\s+-[a-zA-Z]*R\b`, which
    requires R to be immediately followed by a word boundary. `ls -lRa`
    has `a` after R, so the `\b` after R was NOT at a word boundary.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "ls -lRa /tmp",
            "ls -Rla /tmp",
            "ls -aRl /tmp",
            "ls -lRah /tmp",
            "ls -R /tmp",  # existing baseline (must still match)
            "ls -laR /tmp",  # R at end (must still match)
        ],
    )
    def test_f7_ls_recursive_variants_blocked_for_main_agent(self, command: str) -> None:
        assert _ORCHESTRATOR_HEAVY_BASH_RE.search(command) is not None, (
            f"recursive ls variant must match heavy-bash RE: {command!r}"
        )
        result = handle_enforce_orchestrator_boundary(_main_agent_bash(command))
        assert result is True, f"main-agent {command!r} must be blocked"

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la /tmp",
            "ls /tmp",
            "ls -l /tmp",
            "ls -a /tmp",
        ],
    )
    def test_f7_non_recursive_ls_not_blocked(self, command: str) -> None:
        result = handle_enforce_orchestrator_boundary(_main_agent_bash(command))
        assert result is not True, f"non-recursive ls must not be blocked: {command!r}"


# ── F8: git push pipeline auto-merge bypass ─────────────────────────────


class TestF8PipelineAutoMergeBypass:
    """`git push -o merge_request.merge_when_pipeline_succeeds` schedules auto-merge.

    This push option instructs GitLab to merge the MR automatically when CI
    passes, making it semantically equivalent to `glab mr merge` but via a
    git-push flag that was not covered by any existing blocked pattern.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git push -o merge_request.merge_when_pipeline_succeeds origin main",
            "git push --push-option=merge_request.merge_when_pipeline_succeeds origin main",
            "git push -o merge_request.merge_when_pipeline_succeeds",
        ],
    )
    def test_f8_pipeline_auto_merge_push_is_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True, f"pipeline auto-merge push must be blocked: {command!r}"
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            # quoted value forms — were a bypass before the fix
            'git push -o "merge_request.merge_when_pipeline_succeeds" origin main',
            "git push -o 'merge_request.merge_when_pipeline_succeeds' origin main",
            'git push --push-option="merge_request.merge_when_pipeline_succeeds" origin main',
        ],
    )
    def test_f8_quoted_pipeline_auto_merge_push_is_blocked(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is True, f"quoted pipeline auto-merge push must be blocked: {command!r}"
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin main",
            "git push -u origin feature",
            "git push --force-with-lease origin feature",
            "git push -o merge_request.create origin feature",
            "git push -o merge_request.draft origin feature",
            'git push -o "merge_request.create" origin feature',
        ],
    )
    def test_f8_normal_push_not_blocked(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_block_direct_commands(_bash_event(command))
        assert result is not True, f"normal push must not be blocked: {command!r}"
        assert capsys.readouterr().out.strip() == ""
