"""Local bare-git-origin fixtures shared by the fleet-claim wiring tests.

A ``file://`` push against a ``git init --bare`` origin exercises the same
receive-pack ref transaction (server-side CAS) as a GitHub push, with no network.
Not a test module — no ``test_`` prefix, so pytest never collects it.
"""

import subprocess
from pathlib import Path


def _run(*args: str) -> str:
    cmd = ["git", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def git(repo: Path, *args: str) -> str:
    return _run("-C", str(repo), *args)


def init_bare(path: Path) -> Path:
    _run("init", "--bare", "-q", str(path))
    return path


def _credential_check(token: str, on_match: str) -> str:
    return (
        f'#!/bin/sh\n[ "$GH_TOKEN" = "{token}" ] && {on_match}\n'
        'echo "credential refused: ${GH_TOKEN:-none}" >&2\nexit 1\n'
    )


def require_credential(bare: Path, token: str) -> Path:
    """Make *bare* refuse every ref update whose pusher did not carry ``GH_TOKEN=<token>``.

    A ``file://`` receive-pack inherits the pusher's environment, so the credential a
    claim hands git is exactly what this pre-receive hook sees.
    """
    hook = bare / "hooks" / "pre-receive"
    hook.write_text(_credential_check(token, "exit 0"), encoding="utf-8")
    hook.chmod(0o755)
    return bare


def init_client(client_dir: Path, bare: Path) -> Path:
    _run("init", "-q", str(client_dir))
    git(client_dir, "remote", "add", "origin", f"file://{bare}")
    return client_dir


def private_origin(tmp: Path, token: str) -> tuple[Path, Path]:
    """A bare origin and a client of it that refuse every push, ls-remote and fetch not carrying ``GH_TOKEN=<token>``.

    A ``file://`` read is served by the client's ``remote.origin.uploadpack``, so gating it models a private repo.
    """
    bare = require_credential(init_bare(tmp / "origin.git"), token)
    client = init_client(tmp / "client", bare)
    upload_pack = client / ".git" / "upload-pack-requiring-credential"
    upload_pack.write_text(_credential_check(token, 'exec git upload-pack "$@"'), encoding="utf-8")
    upload_pack.chmod(0o755)
    git(client, "config", "remote.origin.uploadpack", str(upload_pack))
    return bare, client


def ref_sha(bare: Path, ref: str) -> str:
    return git(bare, "for-each-ref", "--format=%(objectname)", ref).strip()


def init_with_origin(path: Path, origin_url: str) -> Path:
    """A git repo with ``origin`` set to *origin_url* (unreachable is fine — only its slug is read)."""
    _run("init", "-q", str(path))
    git(path, "remote", "add", "origin", origin_url)
    return path
