"""Git helpers."""

import subprocess


def default_branch(repo: str = ".") -> str:
    """Detect the default remote branch for a repo.

    Returns branch name (e.g. 'master') or raises RuntimeError.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch = result.stdout.strip().replace("refs/remotes/origin/", "")
        if branch:
            return branch
    except subprocess.CalledProcessError:
        pass

    for candidate in ("master", "main", "development"):
        result = subprocess.run(
            [
                "git",
                "-C",
                repo,
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{candidate}",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            return candidate

    msg = f"Could not detect default branch for {repo}"
    raise RuntimeError(msg)
