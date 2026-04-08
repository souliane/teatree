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


def branch_merged(repo: str, branch: str, target: str = "origin/main") -> bool:
    """Return True if *branch* has been merged into *target*."""
    output = run(repo=repo, args=["branch", "--merged", target])
    return any(line.strip() == branch for line in output.splitlines())


def current_branch(repo: str = ".") -> str:
    result = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def remote_url(repo: str = ".", remote: str = "origin") -> str:
    """Return the fetch URL for the given remote, or empty string if not found."""
    return run(repo=repo, args=["remote", "get-url", remote])


def config_value(repo: str = ".", key: str = "") -> str:
    """Return a git config value, or empty string if not set."""
    return run(repo=repo, args=["config", key])


def last_commit_message(repo: str = ".") -> tuple[str, str]:
    """Return ``(subject, body)`` from the last git commit."""
    output = run(repo=repo, args=["log", "-1", "--format=%s%n%n%b"])
    lines = output.split("\n", 1)
    subject = lines[0].strip()
    body = lines[1].strip() if len(lines) > 1 else ""
    return subject, body


def worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
    """Add a git worktree. Returns True on success."""
    args = ["worktree", "add"]
    if create_branch:
        args.extend(["-b", branch])
    args.append(path)
    if not create_branch:
        args.append(branch)
    return check(repo=repo, args=args)
