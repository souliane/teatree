import pytest

from teatree.provisioning.skill_source import owner_repo, parse_skill_source, pinned_commit

_SHA = "1f20bef3f59b85ad7b52718f822e37c4478a3ff5"


def test_owner_repo_subpath_and_ref_are_split() -> None:
    source = parse_skill_source("souliane/skills/ac-python#d0008a3")

    assert source is not None
    assert (source.owner_repo, source.subpath, source.ref) == ("souliane/skills", "ac-python", "d0008a3")


def test_a_bundle_dependency_without_a_subpath_names_no_single_skill() -> None:
    assert parse_skill_source("vendor/bundle#1f20bef") is None


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (f"o/r/s#{_SHA}", _SHA),
        (f"o/r/s#{_SHA.upper()}", _SHA),
        (f"o/r#{_SHA}", _SHA),
        (f"o/r/s# {_SHA} ", _SHA),
        (f"o/r/s#{_SHA[:7]}", ""),
        (f"o/r/s#{_SHA}0", ""),
        (f"o/r/s#{_SHA[:-1]}", ""),
        (f"o/r/s#{_SHA}x", ""),
        ("o/r/s#main", ""),
        ("o/r/s#", ""),
        ("o/r/s", ""),
    ],
)
def test_pinned_commit_is_the_lowercase_full_hex_or_empty(spec: str, expected: str) -> None:
    assert pinned_commit(spec) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("o/r/s#abc", "o/r"), ("o/r", "o/r"), ("/o/r/s/deeper#abc", "o/r"), ("o", "o")],
)
def test_owner_repo_is_the_first_two_segments_whatever_the_spec_shape(spec: str, expected: str) -> None:
    assert owner_repo(spec) == expected
