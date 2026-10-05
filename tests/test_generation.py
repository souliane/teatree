"""``teatree.generation`` — the running process's immutable code generation, read from the image."""

from unittest.mock import patch

import pytest

from teatree.generation import (
    current_generation,
    generation_image,
    in_place_update_refusal,
    is_generation_sha,
    is_image_generation,
    short_sha,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_an_unset_generation_is_the_legacy_empty_string() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert current_generation() == ""
        assert not is_image_generation()
        assert in_place_update_refusal() == ""


def test_the_generation_comes_from_the_image_environment() -> None:
    with patch.dict("os.environ", {"TEATREE_GENERATION": f" {SHA}\n"}, clear=True):
        assert current_generation() == SHA
        assert is_image_generation()


def test_an_image_generation_refuses_in_place_updates_naming_itself() -> None:
    with patch.dict("os.environ", {"TEATREE_GENERATION": SHA}, clear=True):
        assert in_place_update_refusal() == "image generation 0123456789ab: code changes by roll, not in place"


def test_the_image_tag_is_derived_from_the_sha() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert generation_image(SHA) == f"teatree-factory:{SHA}"
    assert short_sha(SHA) == "0123456789ab"


def test_a_registry_repository_names_the_image_a_cloud_box_pulls() -> None:
    with patch.dict("os.environ", {"TEATREE_IMAGE_REPOSITORY": "registry.example/team/teatree-factory"}, clear=True):
        assert generation_image(SHA) == f"registry.example/team/teatree-factory:{SHA}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (SHA, True),
        (SHA.upper(), False),
        (SHA[:39], False),
        (f"{SHA}0", False),
        ("main", False),
        ("", False),
    ],
)
def test_only_a_full_lowercase_commit_sha_names_a_generation(value: str, *, expected: bool) -> None:
    assert is_generation_sha(value) is expected
