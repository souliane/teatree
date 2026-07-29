"""The `issue_url` shape classifier behind the silent-freeze probe (souliane/teatree#3492)."""

import pytest

from teatree.core.forge_url import is_forge_url, is_synthetic_ticket_url


@pytest.mark.parametrize(
    "url",
    ["https://github.com/souliane/teatree/issues/3274", "http://gitlab.example.com/acme/app/-/issues/7"],
)
def test_forge_urls_are_real_and_not_synthetic(url: str) -> None:
    assert is_forge_url(url)
    assert not is_synthetic_ticket_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "architectural-review://t3-teatree",
        "eval-local://t3-teatree",
        "scanning-news://t3-teatree",
        "dogfood-smoke://t3-teatree",
        "3274",
    ],
)
def test_cadence_anchors_and_bare_numbers_are_synthetic(url: str) -> None:
    assert is_synthetic_ticket_url(url)
    assert not is_forge_url(url)


@pytest.mark.parametrize("url", ["", "auto:feat/some-branch"])
def test_local_anchors_are_deliverable_work_not_synthetic(url: str) -> None:
    # Excluding these would trade #3492's false positive for a false negative:
    # a branch-anchored ticket carries real work with no forge issue behind it.
    assert not is_synthetic_ticket_url(url)
    assert not is_forge_url(url)
