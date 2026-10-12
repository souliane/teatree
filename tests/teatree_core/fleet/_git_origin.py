"""Local bare-git-origin fixtures shared by the fleet-claim wiring tests.

A ``file://`` push against a ``git init --bare`` origin exercises the same
receive-pack ref transaction (server-side CAS) as a GitHub push, with no network.
Not a test module — no ``test_`` prefix, so pytest never collects it.
"""

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

FIXTURE_IDENTITY = (
    "-c",
    "user.name=teatree-fixture",
    "-c",
    "user.email=teatree-fixture@users.noreply.github.com",  # privacy-scan:allow placeholder noreply fixture identity
)


def _run(*args: str) -> str:
    cmd = ["git", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def git(repo: Path, *args: str) -> str:
    return _run("-C", str(repo), *args)


def empty_tree(repo: Path) -> str:
    return git(repo, "hash-object", "-t", "tree", "-w", "/dev/null")


def init_bare(path: Path) -> Path:
    """A bare origin whose default branch ``main`` holds one root commit, as every forge repo does."""
    _run("init", "--bare", "-q", str(path))
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    root = git(path, *FIXTURE_IDENTITY, "commit-tree", empty_tree(path), "-m", "root")
    git(path, "update-ref", "refs/heads/main", root)
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
    _run("init", "-q", "-b", "main", str(client_dir))
    git(client_dir, "remote", "add", "origin", f"file://{bare}")
    if bare.exists():
        git(client_dir, "fetch", "-q", "origin")
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


@contextmanager
def as_other_instance(data_home: Path) -> Iterator[None]:
    """Act as a separate teatree installation: ``instance_id()`` resolves its own machine data dir."""
    with patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}):
        yield


def ref_sha(bare: Path, ref: str) -> str:
    return git(bare, "for-each-ref", "--format=%(objectname)", ref).strip()


def init_with_origin(path: Path, origin_url: str) -> Path:
    """A git repo with ``origin`` set to *origin_url* (unreachable is fine — only its slug is read)."""
    _run("init", "-q", str(path))
    git(path, "remote", "add", "origin", origin_url)
    return path
