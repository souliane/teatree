from teatree.utils.git import remote_slug


def guard_repo_remote_slug(repo: str, expected_slug: str) -> None:
    actual = remote_slug(repo=repo)
    if actual != expected_slug:
        msg = (
            f"repo remote slug mismatch: expected {expected_slug!r} but "
            f"got {actual!r} — refusing to proceed in the wrong repo"
        )
        raise ValueError(msg)
