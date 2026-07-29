"""The ``workspace stamp-identity`` body — public-clone git identity, not lifecycle (#762).

Its own module for the reason every sibling here has one: the CLI method stays a thin
wrapper so :mod:`teatree.core.management.commands.workspace` remains under the
module-health LOC cap. The concern is genuinely separate too — this is about which
identity a PUBLIC clone's commits carry, and shares nothing with worktree
provisioning, starting or reaping.
"""

from teatree.core.public_identity import StampResult, is_public_github_remote, set_local_noreply_identity
from teatree.utils import git


def run_stamp_identity(repo: str) -> StampResult:
    """Stamp the scoped noreply git identity onto an existing public GitHub clone.

    Idempotent, and refuses any non-public-GitHub remote so a private overlay's (or a
    GitLab clone's) legitimate real-identity attribution is never touched.

    #2655: the visibility gate must see the FULL remote URL, host intact — a
    host-stripped slug would resolve a GitLab clone's bare ``owner/repo`` against
    github.com. ``slug`` is kept only for the human-readable result.
    """
    url = git.remote_url(repo)
    slug = git.remote_slug(repo)
    if not is_public_github_remote(url):
        return StampResult(
            stamped=False,
            reason=f"not a public GitHub remote (slug={slug!r}) — noreply-identity stamping not required",
        )
    set_local_noreply_identity(repo)
    return StampResult(stamped=True, repo=repo, slug=slug)


__all__ = ["StampResult", "run_stamp_identity"]
