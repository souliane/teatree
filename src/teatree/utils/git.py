import subprocess


def run(*, repo: str = ".", args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def check(*, repo: str = ".", args: list[str]) -> bool:
    return (
        subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def default_branch(repo: str = ".") -> str:
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

    for candidate in ("main", "master", "development"):
        result = subprocess.run(
            ["git", "-C", repo, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate

    msg = f"Could not detect default branch for {repo}"
    raise RuntimeError(msg)


def current_branch(repo: str = ".") -> str:
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
