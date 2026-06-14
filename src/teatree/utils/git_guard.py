from teatree.utils import git


def is_github_slug(value: str) -> bool:
    """True iff ``value`` is a bare ``owner/repo`` slug, not a filesystem path.

    A ticket's ``repos`` entry may be either an ``owner/repo`` slug
    (``souliane/teatree``) or a bare basename (``teatree``) the clone
    resolver expands by scanning. Only the slug form carries a canonical
    remote identity to guard against — this predicate lets callers tell the
    two apart without a git invocation.
    """
    owner, sep, name = value.partition("/")
    return bool(sep) and bool(owner) and bool(name) and "/" not in name


def guard_repo_remote_slug(repo: str, expected_slug: str) -> None:
    actual = git.remote_slug(repo=repo)
    if actual != expected_slug:
        msg = (
            f"repo remote slug mismatch: expected {expected_slug!r} but "
            f"got {actual!r} — refusing to proceed in the wrong repo"
        )
        raise ValueError(msg)
