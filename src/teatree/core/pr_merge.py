"""Author-controlled squash-merge for public souliane/* PRs (#762).

A plain ``gh pr merge --squash`` without ``--author-email`` lets GitHub
derive the squash commit author from the merging account's configured
commit email, which is non-deterministic across accounts. This helper
always passes an explicit ``users.noreply.github.com`` ``--author-email``
on public souliane/* and FAILS CLOSED if the resulting squash commit
author is not a noreply address (the synchronous path verifies the
landed author via ``gh api``; the #730 pre-push check only sees branch
commits, not the server-side squash). Non-souliane / private remotes
are merged unchanged — their configured identity is left as-is.
"""

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


def _merge_sha(slug: str, pr: int) -> str:
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr), "--repo", slug, "--json", "mergeCommit", "--jq", ".mergeCommit.oid"],
    )
    return out.strip() if rc == 0 else ""


def squash_merge_public(*, pr: int, slug: str, auto: bool = False) -> None:
    """Squash-merge with a forced noreply author on public souliane/*.

    Synchronous (``auto=False``): the landed squash author is verified
    via ``gh api`` and the call fails closed if it is non-noreply.

    ``auto=True``: the server-side squash commit does not exist yet, so
    only the ``--author-email`` is forced — there is NO post-merge
    landed-author verification, and the #730 pre-push check cannot see
    the eventual server-side commit either. Prefer the synchronous path
    when the landed-author guarantee must actually be proven on the
    target branch (e.g. a bootstrap merge).

    Non-souliane / private remotes are merged unchanged.
    """
    merge_argv = ["pr", "merge", str(pr), "--repo", slug, "--squash"]
    if auto:
        merge_argv.append("--auto")

    public = is_public_souliane_remote(slug)
    if public:
        _, email = canonical_noreply_identity()
        merge_argv += ["--author-email", email]

    rc, _out, err = _run_gh(merge_argv)
    if rc != 0:
        msg = f"squash-merge of {slug}#{pr} failed: {err.strip() or 'gh pr merge non-zero'}"
        raise RuntimeError(msg)

    if not public or auto:
        # Non-souliane/private, or --auto (the server-side squash is
        # created later, so it cannot be inspected here): the only
        # control applied is the forced --author-email. The landed-author
        # verification below runs only on the synchronous public path.
        return

    sha = _merge_sha(slug, pr)
    if not sha:
        msg = f"could not resolve the squash commit SHA for {slug}#{pr} to verify its author"
        raise MergeAuthorMismatchError(msg)
    rc, author, _ = _run_gh(
        ["api", f"repos/{slug}/commits/{sha}", "--jq", ".commit.author.email"],
    )
    author = author.strip()
    if rc != 0 or not is_noreply_email(author):
        msg = (
            f"squash commit {sha[:8]} on public {slug} has a non-noreply author "
            f"— author verification failed (#762). Halting."
        )
        raise MergeAuthorMismatchError(msg)
