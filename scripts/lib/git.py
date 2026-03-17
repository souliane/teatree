"""Git helpers."""

import subprocess


def run(*, repo: str = ".", args: list[str]) -> str:
    """Run a git command and return stripped stdout."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def check(*, repo: str = ".", args: list[str]) -> bool:
    """Run a git command and return True if it succeeds."""
    return (
        subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


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
