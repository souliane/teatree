from teatree.provisioning.skill_source import parse_skill_source


def test_owner_repo_subpath_and_ref_are_split() -> None:
    source = parse_skill_source("souliane/skills/ac-python#d0008a3")

    assert source is not None
    assert (source.owner_repo, source.subpath, source.ref) == ("souliane/skills", "ac-python", "d0008a3")


def test_a_bundle_dependency_without_a_subpath_names_no_single_skill() -> None:
    assert parse_skill_source("vendor/bundle#1f20bef") is None
