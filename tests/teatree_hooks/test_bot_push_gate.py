import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.config.cold_reader import SettingRead
from teatree.hooks import bot_push_gate
from teatree.hooks.remote_holds import RemoteHoldings


def _git(repo: Path, *args: str) -> str:
    executable = shutil.which("git") or "/usr/bin/git"
    return subprocess.run(
        [executable, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _push_repo(repo: Path, *, author: str, base_author: str = "build-bot") -> tuple[str, str]:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", base_author)
    _git(repo, "config", "user.email", f"{base_author}@users.noreply.github.com")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", "user.name", author)
    _git(repo, "config", "user.email", f"{author}@users.noreply.github.com")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "new")
    return base, _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "case",
    [
        ("https://github.com/build-bot/engine.git", "build-bot", "build-bot", 0),
        ("https://github.com/build-bot/engine.git", "build-bot", "person", 1),
        ("https://github.com/build-bot/engine.git", "person", "build-bot", 1),
        ("https://github.com/team/engine.git", "person", "person", 0),
        ("https://github.com/build-bot/engine.git", "build-bot", "", 1),
    ],
)
def test_pre_push_identity_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str, str, int],
) -> None:
    remote, author, login, expected = case
    base, head = _push_repo(tmp_path, author=author)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"refs/heads/main {head} refs/heads/main {base}\n"))
    monkeypatch.setattr(
        bot_push_gate.cold_reader,
        "read_setting_confirmed",
        lambda key: SettingRead({"github.com": ["build-bot"]}, readable=True),
    )
    monkeypatch.setattr(bot_push_gate, "_credential_login", lambda forge, host, url, token, **kwargs: login)
    monkeypatch.setattr(
        bot_push_gate,
        "remote_holdings",
        lambda repo, remote_url, remote_name, reported_shas, env_override: RemoteHoldings(tips=(base,), listed=True),
    )

    result = bot_push_gate.main(["origin", remote])

    assert result == expected
    output = capsys.readouterr()
    if expected:
        assert "refuse" in output.err.lower()
    else:
        assert not output.err


def test_pre_push_hook_is_installed() -> None:
    config = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"
    assert "id: refuse-bot-push" in config.read_text()


def test_proposed_commit_requires_bot_author_and_committer(tmp_path: Path) -> None:
    _push_repo(tmp_path, author="build-bot")
    assert bot_push_gate._proposed_bot_commit(tmp_path, "build-bot")
    _git(tmp_path, "config", "user.name", "person")
    assert not bot_push_gate._proposed_bot_commit(tmp_path, "build-bot")


@pytest.mark.parametrize(
    "case",
    [
        ("https://github.com/build-bot/engine.git", "github.com", "github", "GH_TOKEN"),
        ("https://gitlab.example.test/build-bot/engine.git", "gitlab.example.test", "gitlab", "GITLAB_TOKEN"),
    ],
)
def test_routed_bot_token_lists_private_remote_before_scanning_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: tuple[str, str, str, str]
) -> None:
    remote, host, forge, token_name = case
    base, head = _push_repo(tmp_path, author="build-bot", base_author="person")
    monkeypatch.setattr(
        bot_push_gate.cold_reader,
        "read_setting_confirmed",
        lambda key: SettingRead({host: ["build-bot"]}, readable=True),
    )

    def credential_login(actual_forge: str, actual_host: str, url: str, token: str, *, repo: Path) -> str:
        assert (actual_forge, actual_host, url, token) == (forge, host, remote, "bot-key")
        assert repo == tmp_path
        return "build-bot"

    monkeypatch.setattr(bot_push_gate, "_credential_login", credential_login)

    def holdings(
        repo: Path, *, remote_url: str, remote_name: str, reported_shas: tuple[str, ...], env_override: dict[str, str]
    ) -> RemoteHoldings:
        assert env_override == {token_name: "bot-key"}
        return RemoteHoldings(tips=(base,), listed=True)

    monkeypatch.setattr(bot_push_gate, "remote_holdings", holdings)

    assert (
        bot_push_gate.bot_push_error(
            repo=tmp_path, remote_name="origin", remote_url=remote, refs=([(head, "")], True), token="bot-key"
        )
        == ""
    )


def test_credential_probe_uses_the_username_in_the_push_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    helper = tmp_path / "credential-helper"
    helper.write_text(
        "#!/bin/sh\n"
        '[ "$1" = get ] || exit 1\n'
        'case "$(cat)" in\n'
        "  *username=oauth2*) printf 'username=oauth2\\npassword=bot-key\\n' ;;\n"
        "  *) printf 'username=person\\npassword=person-key\\n' ;;\n"
        "esac\n"
    )
    helper.chmod(0o755)
    _git(tmp_path, "config", "credential.helper", str(helper))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$GH_TOKEN" = bot-key ]; then\n'
        "  printf '%s\\n' '{\"login\":\"build-bot\"}'\n"
        "else\n"
        "  printf '%s\\n' '{\"login\":\"person\"}'\n"
        "fi\n"
    )
    gh.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    assert (
        bot_push_gate._credential_login(
            "github",
            "github.com",
            "https://oauth2@github.com/build-bot/engine.git",  # privacy-scan:allow synthetic URL userinfo
        )
        == "build-bot"
    )
    assert (
        bot_push_gate._credential_login(
            "github",
            "github.com",
            "https://person:person-key@github.com/build-bot/engine.git",  # privacy-scan:allow synthetic URL userinfo
            "bot-key",
        )
        == ""
    )


@pytest.mark.parametrize(("login", "expected"), [("build-bot", 0), ("person", 1), ("", 1)])
def test_ambient_gitlab_token_must_authenticate_as_a_declared_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], login: str, expected: int
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "ambient-key")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.example.test")
    monkeypatch.setattr(bot_push_gate, "_bot_logins", lambda host: frozenset({"build-bot"}))
    monkeypatch.setattr(bot_push_gate, "_credential_login", lambda forge, host, url, token: login)

    assert bot_push_gate.main(["--verify-ambient"]) == expected
    if expected:
        assert "bot credential" in capsys.readouterr().err


def test_ambient_gitlab_token_refuses_unreadable_bot_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "ambient-key")
    monkeypatch.setattr(bot_push_gate, "_bot_logins", lambda host: None)

    assert bot_push_gate.main(["--verify-ambient"]) == 1
