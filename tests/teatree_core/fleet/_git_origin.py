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


def init_client(client_dir: Path, bare: Path) -> Path:
    _run("init", "-q", str(client_dir))
    git(client_dir, "remote", "add", "origin", f"file://{bare}")
    return client_dir


def ref_sha(bare: Path, ref: str) -> str:
    return git(bare, "for-each-ref", "--format=%(objectname)", ref).strip()


def init_with_origin(path: Path, origin_url: str) -> Path:
    """A git repo with ``origin`` set to *origin_url* (unreachable is fine — only its slug is read)."""
    _run("init", "-q", str(path))
    git(path, "remote", "add", "origin", origin_url)
    return path
