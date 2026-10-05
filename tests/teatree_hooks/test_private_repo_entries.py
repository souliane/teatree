"""Host-qualified private repository entries keep forge and segment boundaries."""

from pathlib import Path

import pytest

from teatree.hooks import _private_repo_entries, _repo_visibility


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://GITLAB.COM/acme-eng/widget.git/", True),
        ("https://git:secret@gitlab.com:443/acme-eng/widget.git", True),
        ("ssh://git@gitlab.com:2222/acme-eng/widget.git", True),
        ("git@gitlab.com:/acme-eng/widget.git", True),
        ("https://github.com/acme-eng/oss-lib", False),
        ("https://gitlab.com/acme-eng-oss/x", False),
    ],
)
def test_matcher_normalizes_and_keeps_host_and_segment_boundary(remote: str, expected: object) -> None:
    assert (
        _private_repo_entries.private_repo_entry_matches(
            "gitlab.com/acme-eng", remote, normalize=_repo_visibility.slug_for_remote_url
        )
        is expected
    )


@pytest.mark.parametrize("slug", ["acme-eng/widget", "intranet/acme-eng/widget"])
def test_a_slug_without_a_dotted_host_is_never_judged(
    slug: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a real forge host neither the probe nor the allowlist can say whose repo this is.
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "config.sqlite3"))
    asked: list[str] = []

    def probe(qualified: str) -> str:
        asked.append(qualified)
        return "PRIVATE"

    monkeypatch.setattr(_repo_visibility, "slug_visibility", probe)
    monkeypatch.setattr(_repo_visibility, "slug_is_allowlisted_private", lambda *_args: True)

    assert _private_repo_entries.private_repo_visibility(slug, ops=_repo_visibility) is None
    assert asked == []
