"""Refuse a push to a declared bot namespace under a human credential or commit identity."""

import json
import os
import sys
from dataclasses import dataclass
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from teatree.config import cold_reader
from teatree.hooks._forge_tool import FORGE_TOOL, GITHUB, GITLAB, forge_and_repo_path, host_of_slug
from teatree.hooks._repo_visibility import slug_for_remote_url
from teatree.hooks.remote_holds import remote_holdings
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_ZERO = "0" * 40
_PROBE_TIMEOUT_SECONDS = 8
HTTP_OK = 200
_REF_FIELDS = 4
_COMMIT_FIELDS = 4


class BotIdentityUnreadableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BotRepo:
    slug: str
    host: str
    forge: str
    repo_path: str
    login: str


def _git(repo: Path, *args: str, stdin: str = "") -> str | None:
    try:
        result = run_allowed_to_fail(
            ["git", *args],
            cwd=repo,
            stdin_text=stdin,
            timeout=_PROBE_TIMEOUT_SECONDS,
            expected_codes=None,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
        )
    except (OSError, TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _credential_login(forge: str, host: str, remote_url: str, token: str = "", *, repo: Path | None = None) -> str:
    parsed = urlsplit(remote_url)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.password is not None:
        return ""
    if not token:
        username = f"username={parsed.username}\n" if parsed.username else ""
        credential_input = f"protocol=https\nhost={host}\n{username}path={parsed.path.lstrip('/')}\n\n"
        credentials = _git(repo or Path.cwd(), "credential", "fill", stdin=credential_input)
        if credentials is None:
            return ""
        fields = dict(line.split("=", 1) for line in credentials.splitlines() if "=" in line)
        token = fields.get("password", "")
    if not token:
        return ""
    try:
        if forge == GITLAB:
            connection = HTTPSConnection(host, timeout=_PROBE_TIMEOUT_SECONDS)
            try:
                connection.request("GET", "/api/v4/user", headers={"Authorization": f"Bearer {token}"})
                response = connection.getresponse()
                if response.status != HTTP_OK:
                    return ""
                payload = json.load(response)
            finally:
                connection.close()
        else:
            result = run_allowed_to_fail(
                [FORGE_TOOL[forge], "api", "user", "--hostname", host],
                expected_codes=None,
                env={**os.environ, "GH_TOKEN": token},
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, TimeoutExpired, ValueError):
        return ""
    key = "login" if forge == GITHUB else "username"
    login = payload.get(key) if isinstance(payload, dict) else None
    return login.strip() if isinstance(login, str) else ""


def _updates(text: str) -> list[tuple[str, str]]:
    updates: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == _REF_FIELDS and parts[1] != _ZERO:
            updates.append((parts[1], parts[3]))
    return updates


def _new_commits(repo: Path, local_sha: str, held: tuple[str, ...]) -> list[str] | None:
    result = _git(repo, "rev-list", local_sha, *(["--not", *held] if held else []))
    return result.splitlines() if result is not None else None


def _bot_email(email: str, login: str) -> bool:
    local = email.partition("@")[0].casefold()
    if local.partition("+")[0].isdigit():
        local = local.partition("+")[2]
    return local == login


def _bot_commit(repo: Path, sha: str, login: str) -> bool:
    raw = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", sha)
    if raw is None:
        return False
    identity = raw.strip().split("\x00")
    if len(identity) != _COMMIT_FIELDS:
        return False
    author, author_email, committer, committer_email = identity
    return (
        author.casefold() == login
        and committer.casefold() == login
        and _bot_email(author_email, login)
        and _bot_email(committer_email, login)
    )


def _proposed_bot_commit(repo: Path, login: str) -> bool:
    for role in ("AUTHOR", "COMMITTER"):
        raw = _git(repo, "var", f"GIT_{role}_IDENT")
        if raw is None:
            return False
        name, separator, rest = raw.strip().rpartition(" <")
        if not separator or name.casefold() != login or not _bot_email(rest.partition(">")[0], login):
            return False
    return True


def _push_refs() -> tuple[list[tuple[str, str]], bool]:
    refs_text = sys.stdin.read()
    synthesized = not refs_text.strip()
    if synthesized and (head := os.environ.get("PRE_COMMIT_TO_REF")):
        refs_text = f"HEAD {head} HEAD {os.environ.get('PRE_COMMIT_FROM_REF', _ZERO)}\n"
    return _updates(refs_text), synthesized


def _bot_logins(host: str) -> frozenset[str] | None:
    reading = cold_reader.read_setting_confirmed("self_forge_identities")
    if not reading.readable or (reading.value is not None and not isinstance(reading.value, dict)):
        return None
    declared = (reading.value or {}).get(host)
    if declared is None:
        return frozenset()
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        return None
    return frozenset(item.strip().casefold() for item in declared if item.strip())


def bot_repo_for(remote_url: str) -> BotRepo | None:
    slug = slug_for_remote_url(remote_url)
    host = host_of_slug(slug)
    forge, repo_path = forge_and_repo_path(slug)
    if not host or forge not in {GITHUB, GITLAB}:
        return None
    bot_logins = _bot_logins(host)
    if bot_logins is None:
        message = "bot identity configuration could not be read"
        raise BotIdentityUnreadableError(message)
    login = repo_path.partition("/")[0].casefold()
    if login not in bot_logins:
        return None
    return BotRepo(slug=slug, host=host, forge=forge, repo_path=repo_path, login=login)


def _new_commit_error(
    repo: Path, remote: tuple[str, str], refs: tuple[list[tuple[str, str]], bool], target: BotRepo, token: str
) -> str:
    remote_name, remote_url = remote
    updates, synthesized = refs
    reported = () if synthesized else tuple(remote_sha for _, remote_sha in updates if remote_sha != _ZERO)
    credential_env = {"GH_TOKEN" if target.forge == GITHUB else "GITLAB_TOKEN": token} if token else None
    holdings = remote_holdings(
        repo,
        remote_url=remote_url,
        remote_name=remote_name,
        reported_shas=reported,
        env_override=credential_env,
    )
    slug = slug_for_remote_url(remote_url)
    for local_sha, _ in updates:
        commits = _new_commits(repo, local_sha, holdings.tips)
        if commits is None:
            return f"the new commits for bot-owned {slug} could not be read"
        for sha in commits:
            if not _bot_commit(repo, sha, target.login):
                return f"a push to bot-owned {slug} carries a non-bot commit ({sha[:12]})"
    return ""


def bot_push_error(
    *,
    repo: Path,
    remote_name: str,
    remote_url: str,
    refs: tuple[list[tuple[str, str]], bool],
    token: str = "",
) -> str:
    updates, _ = refs
    if not updates:
        return ""
    try:
        target = bot_repo_for(remote_url)
    except BotIdentityUnreadableError as exc:
        return str(exc)
    if target is None:
        return ""

    login = _credential_login(target.forge, target.host, remote_url, token, repo=repo).casefold()
    if login != target.login:
        return f"a push to bot-owned {target.slug} requires a verified bot credential"
    return _new_commit_error(repo, (remote_name, remote_url), refs, target, token)


def bot_proposed_commit_error(repo: Path, remote_url: str) -> str:
    try:
        target = bot_repo_for(remote_url)
    except BotIdentityUnreadableError as exc:
        return str(exc)
    if target is not None and not _proposed_bot_commit(repo, target.login):
        return f"a commit for bot-owned {target.slug} requires a bot author and committer"
    return ""


def ambient_bot_credential_error() -> str:
    token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        return ""
    try:
        host = urlsplit(os.environ.get("GITLAB_URL", "https://gitlab.com")).hostname or ""
    except ValueError:
        host = ""
    if not host:
        return "ambient GitLab token has no readable forge host"
    declared = _bot_logins(host)
    if not declared:
        return "ambient GitLab token has no readable declared bot identity"
    authenticated = _credential_login(GITLAB, host, f"https://{host}/", token).casefold()
    if authenticated not in declared:
        return "ambient GitLab token is not a verified bot credential"
    return ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--verify-ambient"]:
        error = ambient_bot_credential_error()
        if error:
            sys.stderr.write(f"refuse: {error}\n")
            return 1
        return 0
    remote_name = args[0] if args else os.environ.get("PRE_COMMIT_REMOTE_NAME", "origin")
    remote_url = args[1] if len(args) > 1 else os.environ.get("PRE_COMMIT_REMOTE_URL", "")
    if not remote_url:
        remote_url = (_git(Path.cwd(), "remote", "get-url", "--push", remote_name) or "").strip()
    error = bot_push_error(repo=Path.cwd(), remote_name=remote_name, remote_url=remote_url, refs=_push_refs())
    if error:
        sys.stderr.write(f"refuse: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
