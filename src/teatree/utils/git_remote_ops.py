"""Invoking remote/config operations that shell out to git.

The remote partition of :mod:`teatree.utils.git` that actually spawns a
process — ``git remote get-url`` / ``git config`` — distinct from the pure,
no-I/O URL parsing in :mod:`teatree.utils.git_remote`. Runs through the
:mod:`teatree.utils.git_run` runners.
"""

from teatree.utils.git_run import run


def remote_url(repo: str = ".", remote: str = "origin") -> str:
    return run(repo=repo, args=["remote", "get-url", remote])


def remote_slug(repo: str = ".", remote: str = "origin") -> str:
    if "/" in repo and not repo.startswith("/") and ":" not in repo and "@" not in repo:
        return repo
    url = remote_url(repo=repo, remote=remote)
    if not url:
        return ""
    cleaned = url.rstrip("/")
    cleaned = cleaned.removesuffix(".git")
    if "@" in cleaned and ":" in cleaned and "://" not in cleaned:
        return cleaned.split(":", 1)[1]
    if "://" in cleaned:
        host_and_path = cleaned.split("://", 1)[1].split("/", 1)
        if len(host_and_path) > 1:
            return host_and_path[1]
    return ""


def config_value(repo: str = ".", key: str = "") -> str:
    return run(repo=repo, args=["config", key])
