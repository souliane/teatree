import subprocess


def run(*, repo: str = ".", args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def run_checked(*, repo: str = ".", args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
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


# ── High-level operations ────────────────────────────────────────────


def merge_base(repo: str = ".", target: str = "origin/main") -> str:
    return run_checked(repo=repo, args=["merge-base", target, "HEAD"])


def rev_count(repo: str = ".", range_spec: str = "") -> int:
    out = run_checked(repo=repo, args=["rev-list", "--count", range_spec])
    return int(out)


def log_oneline(repo: str = ".", range_spec: str = "") -> str:
    return run(repo=repo, args=["log", "--oneline", range_spec])


def status_porcelain(repo: str = ".") -> str:
    return run(repo=repo, args=["status", "--porcelain"])


def soft_reset(repo: str = ".", target: str = "") -> None:
    run_checked(repo=repo, args=["reset", "--soft", target])


def commit(repo: str = ".", message: str = "") -> None:
    run_checked(repo=repo, args=["commit", "-m", message])


def fetch(repo: str = ".", remote: str = "origin", ref: str = "") -> None:
    args = ["fetch", remote]
    if ref:
        args.append(ref)
    run(repo=repo, args=args)


def rebase(repo: str = ".", target: str = "") -> None:
    run_checked(repo=repo, args=["rebase", target])


def worktree_remove(repo: str = ".", path: str = "") -> bool:
    return check(repo=repo, args=["worktree", "remove", "--force", path])


def branch_delete(repo: str = ".", branch: str = "") -> bool:
    return check(repo=repo, args=["branch", "-D", branch])


def pull_ff_only(repo: str = ".") -> bool:
    return check(repo=repo, args=["pull", "--ff-only"])


# ── Discovery ────────────────────────────────────────────────────────


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
