"""Tests for the secret-file-print deny gate in hook_router (#2306).

A Bash command that routes a known secret-bearing source to stdout — via
cat/head/tail/printf/echo of credential/config files, or pass show without
redirection, or an echoed token literal — is BLOCKED. Reading into a variable,
piping to a file, or any non-Bash tool passes through. Fails OPEN on parse
error (consistent with the raw-review-post guard).
"""

import json

import pytest

from hooks.scripts.hook_router import handle_block_secret_file_print


def _bash_event(command: str, tool_name: str = "Bash") -> dict:
    return {
        "session_id": "sess-secret-print",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestBlocksSecretFilePrints:
    """Commands that print a known secret-bearing file to stdout are blocked."""

    @pytest.mark.parametrize(
        "command",
        [
            # cat of known secret files
            "cat ~/.teatree.toml",
            "cat $HOME/.teatree.toml",
            'cat "${HOME}/.teatree.toml"',
            "cat ~/.netrc",
            "cat ~/.config/gh/hosts.yml",
            'cat "~/Library/Application Support/glab-cli/config.yml"',
            "cat ~/.config/glab-cli/config.yml",
            "cat ~/.ssh/id_rsa",
            "cat ~/.ssh/id_ed25519",
            "cat .env",
            "cat .env.local",
            "cat .env.production",
            "cat secrets.env",
            "cat my.credentials",
            "cat service_account.json",
            "cat server.pem",
            "cat client.key",
            # head/tail of secret files
            "head ~/.teatree.toml",
            "head -n 5 ~/.netrc",
            "tail -n 20 ~/.config/gh/hosts.yml",
            "tail ~/.ssh/id_rsa",
            # pass show without redirection
            "pass show email/work",
            "pass show -c personal/github",
            "pass show infra/api-key",
            # echo of a pasted token literal
            "echo glpat-abc123xyz",
            "echo ghp_sometoken123",
            "echo gho_oauthtoken",
            "echo xoxb-slack-token",
            "echo xoxp-slack-token",
            "echo sk-openai-key",
            # printf of a token literal
            "printf glpat-secret",
            # cat with absolute path matching secret pattern
            "cat /Users/user/.teatree.toml",
            "cat /home/user/.netrc",
            "cat /root/.ssh/id_rsa",
        ],
    )
    def test_secret_print_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    def test_deny_message_is_actionable(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_block_secret_file_print(_bash_event("cat ~/.teatree.toml"))
        deny = _parse_deny(capsys)
        assert deny is not None
        reason = deny["permissionDecisionReason"]
        assert "BLOCKED" in reason
        assert "secret" in reason.lower() or "credential" in reason.lower()


class TestAllowsBenignCommands:
    """Non-printing uses, variable captures, and file redirections pass through."""

    @pytest.mark.parametrize(
        "command",
        [
            # Extracting into a variable — stdout never carries the secret
            "TOKEN=$(pass show infra/api-key)",
            "export TOKEN=$(pass show infra/api-key)",
            "TOK=$(cat ~/.netrc | grep password | awk '{print $2}')",
            "PASS=$(cat ~/.teatree.toml | grep token | cut -d= -f2)",
            # Piping to a file — value stays off stdout
            "pass show email/work > /tmp/pw.txt",
            "cat ~/.teatree.toml > /tmp/cfg_backup.toml",
            "cat ~/.netrc >> /tmp/backup.txt",
            # Redirecting to /dev/null (test/check pattern)
            "cat ~/.teatree.toml > /dev/null",
            # curl using the token via env/header — value never echoed
            'curl -H "PRIVATE-TOKEN: $TOKEN" https://gitlab.com/api/v4/user',
            'curl -H "Authorization: Bearer $TOKEN" https://api.github.com/user',
            # cat of non-secret files
            "cat README.md",
            "cat src/teatree/core/models.py",
            "cat /etc/hosts",
            "cat ~/.gitconfig",
            "cat ~/.bashrc",
            "head -n 10 README.md",
            "tail -f /var/log/app.log",
            # echo of safe strings (mentioning secret file paths in prose)
            "echo 'Do not cat ~/.teatree.toml'",
            "echo 'The config lives at ~/.teatree.toml'",
            # echo of non-token strings
            "echo hello world",
            "echo 'manage.py runserver is not allowed'",
            # grep for patterns in secret files (doesn't print whole file)
            "grep -c '' ~/.teatree.toml",
            # pass without show — list/edit/generate/etc. are safe
            "pass ls",
            "pass list",
            "pass generate email/new 20",
            "pass edit email/work",
            # wc, stat, ls on secret files — metadata only
            "wc -l ~/.teatree.toml",
            "stat ~/.teatree.toml",
            "ls -la ~/.ssh/",
            # git commands that happen to touch config paths
            "git config --global user.email",
            "git diff HEAD",
            # Unrelated general commands
            "uv run pytest --no-cov -q",
            "ruff check src/",
            "t3 teatree worktree start",
        ],
    )
    def test_command_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is not True
        assert capsys.readouterr().out.strip() == ""

    def test_ignores_non_bash_tools(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event("cat ~/.teatree.toml", tool_name="Read")) is not True

    def test_empty_command_passes_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event("")) is not True


class TestBlocksQuotedTokenLiterals:
    """A token literal inside quotes still prints to the transcript and is blocked."""

    @pytest.mark.parametrize(
        "command",
        [
            'echo "glpat-synthetictoken123"',
            "echo 'ghp_synthetictoken456'",
            'printf "xoxb-synthetic-789"',
            "echo 'sk-syntheticopenaikey'",
        ],
    )
    def test_quoted_token_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            'echo "just some prose"',
            "echo 'The config lives at ~/.teatree.toml'",
            'printf "hello world\\n"',
        ],
    )
    def test_quoted_prose_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is not True
        assert capsys.readouterr().out.strip() == ""


class TestBlocksReEmitterPipes:
    """A pipe whose sink re-displays the secret to the transcript is still blocked."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat .env | grep -v '^#'",
            "cat ~/.teatree.toml | less",
            "cat .env | tee /dev/tty",
            "cat ~/.netrc | cat",
            "cat ~/.teatree.toml | more",
            "head ~/.netrc | tail -n 2",
            "cat .env | tee leak.txt",
        ],
    )
    def test_re_emitter_pipe_is_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "command",
        [
            "cat .env > /tmp/x",
            "VAR=$(cat .env)",
            "cat ~/.teatree.toml > /tmp/cfg_backup.toml",
            "cat .env | gzip > /tmp/x.gz",
        ],
    )
    def test_genuine_capture_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is not True
        assert capsys.readouterr().out.strip() == ""


class TestAllowsCommittedEnvTemplates:
    """Standard committed non-secret env templates pass; a real .env still blocks."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat .env.example",
            "cat .env.sample",
            "cat .env.template",
            "cat .env.dist",
            "head .env.example",
        ],
    )
    def test_env_template_is_allowed(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is not True
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        "command",
        [
            "cat .env",
            "cat .env.local",
            "cat .env.production",
        ],
    )
    def test_real_env_still_denied(self, command: str, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_secret_file_print(_bash_event(command)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
