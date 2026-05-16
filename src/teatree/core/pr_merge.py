"""Deterministic merge with forced author identity for public souliane/* (#764).

Server-side ``gh pr merge --squash`` sets the squash commit author from
the merging account's configured email, independent of the repository's
local git identity — so the author on ``main`` is non-deterministic
across accounts/tokens. For public ``souliane/*`` this performs the
squash LOCALLY and forces the author AND committer to the canonical
``users.noreply.github.com`` identity, making the result deterministic
regardless of any account or config. A post-push author check on the
landed commit is retained as fail-closed defense-in-depth. Non-souliane
/ private remotes keep the server-side ``gh pr merge`` path unchanged.
"""

import os
import shutil

from teatree.core.public_identity import (
    MergeAuthorMismatchError,
    canonical_noreply_identity,
    is_noreply_email,
    is_public_souliane_remote,
)
from teatree.utils.run import run_allowed_to_fail


def _run_gh(argv: list[str]) -> tuple[int, str, str]:
    gh = shutil.which("gh") or "gh"
    result = run_allowed_to_fail([gh, *argv], expected_codes=None)
    return result.returncode, result.stdout, result.stderr


def _run_git(
    args: list[str],
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    full_env = {**os.environ, **(env or {})}
    result = run_allowed_to_fail(["git", *args], expected_codes=None, env=full_env, cwd=cwd)
    return result.returncode, result.stdout, result.stderr


def _assert_origin_is(slug: str, cwd: str) -> None:
    """Fail closed unless ``cwd``'s ``origin`` remote resolves to ``slug``.

    The merge runs from whatever directory the caller invoked it in (the
    review-loop sweeps the backlog from its own dir). Before any mutating
    git op we verify we are operating on a clone of ``slug`` — otherwise
    ``push origin main`` could land on the wrong repository's main.
    """
    rc, out, _ = _run_git(["remote", "get-url", "origin"], cwd=cwd)
    url = out.strip()
    if rc != 0 or not url:
        msg = f"cannot resolve origin in {cwd!r} — refusing to merge {slug} from an unknown repo (#764)"
        raise RuntimeError(msg)
    cleaned = url.rstrip("/").removesuffix(".git")
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1].split("/", 1)[1] if "/" in cleaned.split("://", 1)[1] else cleaned
    elif "@" in cleaned and ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    if cleaned != slug:
        msg = (
            f"origin in {cwd!r} resolves to {cleaned!r}, not {slug!r} — refusing the "
            f"local-squash merge to avoid pushing to the wrong repository (#764)"
        )
        raise RuntimeError(msg)


def _pr_branch(slug: str, pr: int) -> str:
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr), "--repo", slug, "--json", "headRefName", "--jq", ".headRefName"],
    )
    return out.strip() if rc == 0 else ""


def _pr_squash_message(slug: str, pr: int) -> str:
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr), "--repo", slug, "--json", "title,body", "--jq", '.title + "\\n\\n" + (.body // "")'],
    )
    title_body = out.strip() if rc == 0 else ""
    return title_body or f"Merge pull request #{pr}"


def _landed_head_sha(cwd: str | None = None) -> str:
    rc, out, _ = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    return out.strip() if rc == 0 else ""


def _verify_landed_author(slug: str, cwd: str) -> None:
    sha = _landed_head_sha(cwd=cwd)
    if not sha:
        msg = f"could not resolve the landed commit SHA on {slug} main to verify its author"
        raise MergeAuthorMismatchError(msg)
    rc, author, _ = _run_gh(["api", f"repos/{slug}/commits/{sha}", "--jq", ".commit.author.email"])
    author = author.strip()
    if rc != 0 or not is_noreply_email(author):
        msg = (
            f"landed commit {sha[:8]} on public {slug} main has a non-noreply author "
            f"— author verification failed (#764). Halting."
        )
        raise MergeAuthorMismatchError(msg)


def _server_side_merge(pr: int, slug: str, *, auto: bool) -> None:
    argv = ["pr", "merge", str(pr), "--repo", slug, "--squash"]
    if auto:
        argv.append("--auto")
    rc, _out, err = _run_gh(argv)
    if rc != 0:
        msg = f"squash-merge of {slug}#{pr} failed: {err.strip() or 'gh pr merge non-zero'}"
        raise RuntimeError(msg)


def _local_squash_merge(pr: int, slug: str, repo_path: str = "") -> None:
    name, email = canonical_noreply_identity()
    branch = _pr_branch(slug, pr)
    if not branch:
        msg = f"could not resolve the PR head branch for {slug}#{pr}"
        raise RuntimeError(msg)
    message = _pr_squash_message(slug, pr)
    identity_env = {
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
    }

    # F1: every git op is pinned to a single clone of `slug`. The
    # review-loop sweeps the backlog from an arbitrary cwd, so the target
    # clone must be explicit — ``repo_path`` (when given) is that clone;
    # otherwise the process cwd is used. Assert origin == slug there
    # before any mutating op, then resolve the repo toplevel so the
    # squash/commit/push cannot land on the wrong repository.
    base = repo_path or "."
    _assert_origin_is(slug, cwd=base)
    rc, top, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=base)
    repo = top.strip()
    if rc != 0 or not repo:
        msg = f"cannot resolve the repo toplevel for {slug} at {base!r} — refusing the local-squash merge (#764)"
        raise RuntimeError(msg)

    rc, _o, err = _run_git(["fetch", "origin"], cwd=repo)
    if rc != 0:
        msg = f"git fetch failed for {slug}#{pr}: {err.strip()}"
        raise RuntimeError(msg)
    # F2: a failed `switch main` (e.g. main checked out in another
    # worktree) must STOP — continuing would squash/commit/push on the
    # wrong branch.
    rc, _o, err = _run_git(["switch", "main"], cwd=repo)
    if rc != 0:
        msg = f"git switch main failed for {slug} (is main checked out elsewhere?): {err.strip()}"
        raise RuntimeError(msg)
    rc, _o, err = _run_git(["pull", "--ff-only", "origin", "main"], cwd=repo)
    if rc != 0:
        msg = f"git pull --ff-only failed for {slug} main: {err.strip()}"
        raise RuntimeError(msg)
    rc, _o, err = _run_git(["merge", "--squash", f"origin/{branch}"], cwd=repo)
    if rc != 0:
        msg = f"git merge --squash failed for {slug}#{pr}: {err.strip()}"
        raise RuntimeError(msg)
    rc, _o, err = _run_git(
        ["commit", f"--author={name} <{email}>", "-m", message],
        env=identity_env,
        cwd=repo,
    )
    if rc != 0:
        msg = f"git commit failed for {slug}#{pr}: {err.strip()}"
        raise RuntimeError(msg)

    rc, _o, err = _run_git(["push", "origin", "main"], env=identity_env, cwd=repo)
    if rc != 0:
        msg = (
            f"push to {slug} main was rejected ({err.strip() or 'non-zero'}) — "
            f"stopping. A force-push or workaround is NOT performed (#764)."
        )
        raise RuntimeError(msg)

    _verify_landed_author(slug, cwd=repo)
    landed = _landed_head_sha(cwd=repo)[:8]
    _run_gh(["pr", "close", str(pr), "--repo", slug, "--comment", f"Merged via squashed commit {landed}."])


def squash_merge_public(*, pr: int, slug: str, repo_path: str = "", auto: bool = False) -> None:
    """Merge a PR with a deterministic author on public souliane/* (#764).

    Public ``souliane/*``: a LOCAL ``git merge --squash`` + ``git commit``
    with the author and committer forced to the canonical noreply
    identity, then ``git push origin main`` — the author is deterministic
    regardless of any GitHub account / git config. A push rejection
    (protected branch / non-fast-forward) STOPS with an error; no
    force-push, no workaround. The landed commit author is then verified
    via ``gh api`` (fail-closed defense-in-depth). ``auto`` is ignored on
    this path (the local squash is synchronous by construction).

    ``repo_path`` pins all git ops to a specific clone of ``slug`` — the
    review-loop sweeps the backlog from an arbitrary cwd, so the target
    clone must be explicit and deterministic (origin-asserted == slug
    before any mutating op). When empty, the process cwd is used.

    Non-souliane / private remotes: the server-side ``gh pr merge
    --squash`` path, unchanged (their configured identity is fine).
    """
    if is_public_souliane_remote(slug):
        _local_squash_merge(pr, slug, repo_path)
        return
    _server_side_merge(pr, slug, auto=auto)
