import os
import re

from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked

_REMOTE_HOST_RE = re.compile(r"^(?:git@[^:]+:|https?://[^/]+/|ssh://[^/]+/|git://[^/]+/)")
_SSH_HOST_RE = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/]")
_HTTP_HOST_RE = re.compile(r"^(https?)://([^/]+)/")

# ── Low-level runners ───────────────────────────────────────────────


def run(*, repo: str = ".", args: list[str]) -> str:
    result = run_allowed_to_fail(["git", "-C", repo, *args], expected_codes=None)
    return result.stdout.strip()


def run_strict(*, repo: str = ".", args: list[str]) -> str:
    result = run_checked(["git", "-C", repo, *args])
    return result.stdout.strip()


def check(*, repo: str = ".", args: list[str]) -> bool:
    return run_allowed_to_fail(["git", "-C", repo, *args], expected_codes=None).returncode == 0


# ── High-level operations ────────────────────────────────────────────


def merge_base(repo: str = ".", target: str = "origin/main") -> str:
    return run_strict(repo=repo, args=["merge-base", target, "HEAD"])


def rev_count(repo: str = ".", range_spec: str = "") -> int:
    out = run_strict(repo=repo, args=["rev-list", "--count", range_spec])
    return int(out)


def log_oneline(repo: str = ".", range_spec: str = "") -> str:
    return run(repo=repo, args=["log", "--oneline", range_spec])


def unsynced_commits(repo: str, branch: str, target: str = "origin/main") -> list[str]:
    output = run(repo=repo, args=["log", branch, "--not", target, "--oneline"])
    return [line for line in output.splitlines() if line.strip()]


def commits_absent_from_all_remotes(repo: str, branch: str) -> list[str]:
    """Return ``branch`` commits not reachable from ANY ``refs/remotes/*`` ref.

    The data-loss guard for worktree teardown (#706). Unlike
    :func:`unsynced_commits` (which compares against ``origin/main`` only and
    therefore flags pushed-but-unmerged branches), ``--not --remotes`` is empty
    whenever the branch tip was pushed anywhere — to its own remote tracking
    ref, to main, or captured by a squash-merge that was itself pushed. A
    non-empty result means these commits exist on NO remote: removing the
    worktree would destroy them irrecoverably. Returns ``"<sha> <subject>"``
    lines (newest first).

    **Fails closed.** Uses :func:`run_strict` so a non-zero ``git log`` exit
    (invalid/missing branch, corrupt repo, any git error) raises
    ``CommandFailedError`` rather than yielding an empty list. For a data-loss
    guard, "we couldn't determine whether the commits are pushed" must block
    teardown, not allow it. The legitimate empty case (``git log`` exits 0 with
    no output because the branch genuinely has nothing absent from remotes)
    still returns ``[]`` and allows teardown.
    """
    output = run_strict(repo=repo, args=["log", branch, "--not", "--remotes", "--oneline"])
    return [line for line in output.splitlines() if line.strip()]


def status_porcelain(repo: str = ".") -> str:
    return run(repo=repo, args=["status", "--porcelain"])


def status_porcelain_strict(repo: str = ".") -> str:
    """Like :func:`status_porcelain` but raises on a non-zero ``git status`` exit.

    :func:`status_porcelain` swallows git errors and returns whatever (possibly
    empty) stdout it got, so an inconclusive status (lock contention, corrupt
    index, missing dir) is indistinguishable from a genuinely clean tree. For a
    data-loss decision that must fail closed, use this variant: a git error
    raises ``CommandFailedError`` so the caller can treat "couldn't determine"
    as "might be dirty" rather than "clean".
    """
    return run_strict(repo=repo, args=["status", "--porcelain"])


def soft_reset(repo: str = ".", target: str = "") -> None:
    run_strict(repo=repo, args=["reset", "--soft", target])


def commit(repo: str = ".", message: str = "") -> None:
    run_strict(repo=repo, args=["commit", "-m", message])


def fetch(repo: str = ".", remote: str = "origin", ref: str = "") -> None:
    args = ["fetch", remote]
    if ref:
        args.append(ref)
    run(repo=repo, args=args)


def rebase(repo: str = ".", target: str = "") -> None:
    run_strict(repo=repo, args=["rebase", target])


def merge_no_edit(repo: str = ".", target: str = "") -> bool:
    """``git merge --no-edit <target>`` — returns ``True`` on success.

    The branch-currency gate's primitive (#940). Fast-forward is
    preferred (the default ``git merge`` posture); a non-FF merge with
    an empty editor commit is created when fast-forward isn't possible.
    A conflict yields ``False`` — the caller is expected to call
    :func:`merge_abort` to restore the worktree.
    """
    return check(repo=repo, args=["merge", "--no-edit", target])


def merge_abort(repo: str = ".") -> None:
    """``git merge --abort`` — best-effort restore of the pre-merge tree.

    A no-op when no merge is in progress (the command exits non-zero
    but does no harm), so safe to call unconditionally as part of the
    branch-currency gate's conflict-cleanup path.
    """
    check(repo=repo, args=["merge", "--abort"])


def worktree_remove(repo: str = ".", path: str = "") -> bool:
    return check(repo=repo, args=["worktree", "remove", "--force", path])


def branch_delete(repo: str = ".", branch: str = "") -> bool:
    return check(repo=repo, args=["branch", "-D", branch])


def pull_ff_only(repo: str = ".") -> bool:
    return check(repo=repo, args=["pull", "--ff-only"])


def push(repo: str = ".", remote: str = "origin", branch: str = "") -> None:
    args = ["push", "--set-upstream", remote]
    if branch:
        args.append(branch)
    run_strict(repo=repo, args=args)


def bundle_create(repo: str, bundle_path: str, branch: str) -> None:
    """Write a self-contained ``git bundle`` of ``branch`` to ``bundle_path``.

    A bundle carries every commit reachable from the branch tip and is
    restorable with ``git clone <bundle>`` / ``git fetch <bundle>`` — preferred
    over relocating a worktree directory, which leaves git's worktree admin
    pointing at a stale path. Raises ``CommandFailedError`` on failure (the
    caller must not believe a recovery artifact exists when it does not).
    """
    run_strict(repo=repo, args=["bundle", "create", bundle_path, branch])


def _git_env_without_overrides() -> dict[str, str]:
    """Process env with every ``GIT_*`` variable stripped.

    The inline pre-commit ``pytest`` hook runs under an outer ``git commit``
    that exports ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``. Inherited by
    a child ``git`` call these hijack it onto the outer repo. Capture must run
    against the worktree it was pointed at, not whatever the ambient commit is.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def full_worktree_diff(repo: str) -> str:
    """Return a single patch covering staged, unstaged, AND untracked changes.

    ``git diff HEAD`` alone omits untracked files. Marking them intent-to-add
    (``git add -N``) makes them appear in the diff as new-file hunks (without
    staging their content), so a single ``git apply`` of the returned patch
    restores edits and brand-new files alike. The intent-to-add marks are
    harmless: the worktree is about to be removed.

    The prefixes are forced explicitly with ``--src-prefix=a/
    --dst-prefix=b/``: ``git diff`` otherwise honours the caller's git config,
    and a user with ``diff.noprefix=true`` (common) would get a patch with no
    ``a/``/``b/`` prefixes that a plain ``git apply`` cannot restore — total
    loss of the captured work, the exact #835 scenario. Forcing the prefixes
    keeps the patch standard and ``git apply``-able regardless of user config.
    """
    env = _git_env_without_overrides()
    run_checked(["git", "-C", repo, "add", "-A", "-N"], env=env)
    result = run_checked(
        ["git", "-C", repo, "diff", "HEAD", "--binary", "--src-prefix=a/", "--dst-prefix=b/"],
        env=env,
    )
    return result.stdout


# ── Discovery ────────────────────────────────────────────────────────


def default_branch(repo: str = ".") -> str:
    ref = run(repo=repo, args=["symbolic-ref", "refs/remotes/origin/HEAD"])
    branch = ref.replace("refs/remotes/origin/", "")
    if branch:
        return branch

    for candidate in ("main", "master", "development"):
        if check(repo=repo, args=["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"]):
            return candidate

    msg = f"Could not detect default branch for {repo}"
    raise RuntimeError(msg)


def branch_merged(repo: str, branch: str, target: str = "origin/main") -> bool:
    output = run(repo=repo, args=["branch", "--merged", target])
    return any(line.strip() == branch for line in output.splitlines())


def current_branch(repo: str = ".") -> str:
    return run(repo=repo, args=["rev-parse", "--abbrev-ref", "HEAD"])


def head_sha(repo: str = ".") -> str:
    """Return the full 40-char SHA of ``HEAD`` (the code-under-test SHA).

    Used by the e2e work-item provenance recorder (#794) so a run records
    the *exact* commit it tested, not a branch name that drifts.
    """
    return run_strict(repo=repo, args=["rev-parse", "HEAD"])


def worktree_add_at_ref(repo: str, path: str, ref: str) -> bool:
    """Materialise a detached worktree at an explicit ``ref`` (SHA or branch).

    The e2e ladder (#794) provisions each repo at a resolved ref — a recorded
    last-green SHA or ``origin/main`` — not only at a branch HEAD. ``git
    worktree add <path> <ref>`` checks out ``ref`` in a detached HEAD, which
    is exactly what running the e2e against a recorded SHA-set requires.
    """
    return check(repo=repo, args=["worktree", "add", "--detach", path, ref])


def remote_url(repo: str = ".", remote: str = "origin") -> str:
    return run(repo=repo, args=["remote", "get-url", remote])


def slug_from_remote(remote_url: str) -> str:
    """Extract the ``org/repo`` (or ``ns/group/repo``) slug from a git remote URL.

    Pure string helper (no git invocation). Lives in ``utils`` so both
    ``core`` and the management commands can use it without a layering
    violation.
    """
    if not remote_url:
        return ""
    return _REMOTE_HOST_RE.sub("", remote_url.strip()).removesuffix(".git")


def web_base_from_remote(remote_url: str) -> str:
    """Derive the host web origin (``https://host``) from a git remote URL.

    Handles ``git@host:slug.git``, ``ssh://git@host/slug`` and
    ``https://host/slug`` forms. Returns ``""`` when no host can be parsed.
    """
    if not remote_url:
        return ""
    text = remote_url.strip()
    ssh_match = _SSH_HOST_RE.match(text)
    if ssh_match:
        return f"https://{ssh_match.group(1)}"
    http_match = _HTTP_HOST_RE.match(text)
    if http_match:
        return f"{http_match.group(1)}://{http_match.group(2)}"
    return ""


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


def last_commit_message(repo: str = ".") -> tuple[str, str]:
    output = run(repo=repo, args=["log", "-1", "--format=%s%n%n%b"])
    lines = output.split("\n", 1)
    subject = lines[0].strip()
    body = lines[1].strip() if len(lines) > 1 else ""
    return subject, body


def first_commit_message(repo: str = ".", range_spec: str = "") -> tuple[str, str]:
    """Return ``(subject, body)`` of the OLDEST commit in *range_spec*.

    The canonical PR title for a branch is its first (oldest) own commit —
    the same commit a squash-merge keeps as the squash title. Unlike
    :func:`last_commit_message` (which reads ``HEAD`` and so can pick up the
    default branch's head when the wrong ref is checked out), this sources
    explicitly from a range like ``origin/main..my-branch`` and is therefore
    independent of the working tree. An empty/missing range, or a range with
    no commits, yields ``("", "")`` so the caller can fall back safely
    instead of mislabelling the PR.

    A ``%x1f`` unit separator splits subject from body and a ``%x1e`` record
    separator splits commits, so a body containing blank lines never bleeds
    into the next commit.
    """
    if not range_spec:
        return "", ""
    unit_sep, record_sep = "\x1f", "\x1e"
    output = run(repo=repo, args=["log", "--reverse", range_spec, f"--format=%s{unit_sep}%b{record_sep}"])
    records = [chunk for chunk in output.split(record_sep) if chunk.strip()]
    if not records:
        return "", ""
    subject, _, body = records[0].lstrip("\n").partition(unit_sep)
    return subject.strip(), body.strip()


def commit_messages(repo: str = ".", range_spec: str = "") -> list[str]:
    """Return the full message (subject + body) of each commit in *range_spec*.

    Commits are separated by an ASCII record separator so a body that
    itself contains blank lines never splits one commit into two. An
    empty/missing range yields ``[]`` (nothing to scan, not an error) —
    a close-keyword gate over zero commits must pass, not blow up.
    """
    if not range_spec:
        return []
    sep = "\x1e"
    output = run(repo=repo, args=["log", range_spec, f"--format=%B{sep}"])
    return [chunk.strip() for chunk in output.split(sep) if chunk.strip()]


def worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
    args = ["worktree", "add"]
    if create_branch:
        args.extend(["-b", branch])
    args.append(path)
    if not create_branch:
        args.append(branch)
    try:
        run_checked(["git", "-C", repo, *args])
    except CommandFailedError:
        return False
    return True


# ── GitRepo class ────────────────────────────────────────────────────


class GitRepo:
    """OOP wrapper — encapsulates repo path, delegates to module-level functions.

    Module-level functions remain the canonical implementation so that
    ``patch.object(git_mod, "run", ...)`` in tests intercepts all call paths.
    """

    def __init__(self, path: str = ".") -> None:
        self.path = path

    def merge_base(self, target: str = "origin/main") -> str:
        return merge_base(self.path, target)

    def rev_count(self, range_spec: str = "") -> int:
        return rev_count(self.path, range_spec)

    def log_oneline(self, range_spec: str = "") -> str:
        return log_oneline(self.path, range_spec)

    def unsynced_commits(self, branch: str, target: str = "origin/main") -> list[str]:
        return unsynced_commits(self.path, branch, target)

    def status_porcelain(self) -> str:
        return status_porcelain(self.path)

    def soft_reset(self, target: str = "") -> None:
        soft_reset(self.path, target)

    def commit(self, message: str = "") -> None:
        commit(self.path, message)

    def fetch(self, remote: str = "origin", ref: str = "") -> None:
        fetch(self.path, remote, ref)

    def rebase(self, target: str = "") -> None:
        rebase(self.path, target)

    def worktree_remove(self, path: str = "") -> bool:
        return worktree_remove(self.path, path)

    def branch_delete(self, branch: str = "") -> bool:
        return branch_delete(self.path, branch)

    def pull_ff_only(self) -> bool:
        return pull_ff_only(self.path)

    def push(self, remote: str = "origin", branch: str = "") -> None:
        push(self.path, remote, branch)

    def default_branch(self) -> str:
        return default_branch(self.path)

    def branch_merged(self, branch: str, target: str = "origin/main") -> bool:
        return branch_merged(self.path, branch, target)

    def current_branch(self) -> str:
        return current_branch(self.path)

    def head_sha(self) -> str:
        return head_sha(self.path)

    def worktree_add_at_ref(self, path: str, ref: str) -> bool:
        return worktree_add_at_ref(self.path, path, ref)

    def remote_url(self, remote: str = "origin") -> str:
        return remote_url(self.path, remote)

    def remote_slug(self, remote: str = "origin") -> str:
        return remote_slug(self.path, remote)

    def config_value(self, key: str = "") -> str:
        return config_value(self.path, key)

    def last_commit_message(self) -> tuple[str, str]:
        return last_commit_message(self.path)

    def commit_messages(self, range_spec: str = "") -> list[str]:
        return commit_messages(self.path, range_spec)

    def worktree_add(self, path: str, branch: str, *, create_branch: bool = True) -> bool:
        return worktree_add(self.path, path, branch, create_branch=create_branch)
