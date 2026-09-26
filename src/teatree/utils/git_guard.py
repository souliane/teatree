from teatree.utils import git


def is_remote_project_path(value: str) -> bool:
    """True iff ``value`` names a forge PROJECT PATH, not a bare basename or a filesystem path.

    A ticket's ``repos`` entry is either a project path — ``souliane/teatree`` on
    GitHub, ``group/subgroup/repo`` on GitLab, which nests arbitrarily — or a bare
    basename (``teatree``) the clone resolver expands by scanning. Only the path form
    carries a canonical remote identity to guard against, and ``git.remote_slug``
    already returns the full post-host path, so a multi-segment namespace compares
    exactly as a two-segment one does. Restricting this to two segments left every
    GitLab namespace unguarded (#151).
    """
    if not value or value.startswith("/"):
        return False
    segments = value.split("/")
    return len(segments) > 1 and all(segments)


def guard_repo_remote_slug(repo: str, expected_slug: str) -> None:
    actual = git.remote_slug(repo=repo)
    if actual != expected_slug:
        msg = (
            f"repo remote slug mismatch: expected {expected_slug!r} but "
            f"got {actual!r} — refusing to proceed in the wrong repo"
        )
        raise ValueError(msg)
