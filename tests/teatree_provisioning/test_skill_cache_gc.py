"""Unreferenced skill-source checkouts are collected rather than accumulating (#4677).

The cache is keyed per `(source, ref)`, so every pin bump mints a new directory and the
old one is never read again — measured on a live box: a checkout unreferenced since
2026-07-23 was still on disk 7 weeks later. Collection is deliberately conservative:
anything a declaration names, and anything an installed skill symlink resolves into,
is kept even when this venue cannot explain it.
"""

from pathlib import Path

import pytest

from teatree.provisioning.skill_cache_gc import collect_unreferenced


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    root = tmp_path / "skill-sources"
    for name in ("souliane-skills@old", "souliane-skills@current", "obra-superpowers@pinned"):
        (root / name).mkdir(parents=True)
    return root


def _names(paths: list[Path]) -> set[str]:
    return {path.name for path in paths}


class TestCollection:
    def test_a_checkout_no_declaration_names_is_collected(self, cache_root: Path) -> None:
        collected = collect_unreferenced(
            cache_root, referenced={"souliane-skills@current", "obra-superpowers@pinned"}, link_dirs=[]
        )

        assert _names(collected) == {"souliane-skills@old"}
        assert not (cache_root / "souliane-skills@old").exists()

    def test_a_declared_checkout_is_kept(self, cache_root: Path) -> None:
        collect_unreferenced(cache_root, referenced={"souliane-skills@current"}, link_dirs=[])

        assert (cache_root / "souliane-skills@current").is_dir()

    def test_a_checkout_an_installed_skill_resolves_into_is_kept(self, cache_root: Path, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skills.mkdir()
        published = cache_root / "souliane-skills@old" / "ac-python"
        published.mkdir()
        (skills / "ac-python").symlink_to(published)

        collected = collect_unreferenced(cache_root, referenced=set(), link_dirs=[skills])

        # Nothing declares it, but an agent can still load it — deleting it would
        # break a live skill to reclaim a few megabytes.
        assert "souliane-skills@old" not in _names(collected)
        assert (cache_root / "souliane-skills@old").is_dir()

    def test_a_stale_partial_export_is_collected(self, cache_root: Path) -> None:
        stale = "souliane-skills@current.partial.1234"  # privacy-scan:allow fixture cache dir, not an address
        (cache_root / stale).mkdir()

        collected = collect_unreferenced(cache_root, referenced={"souliane-skills@current"}, link_dirs=[])

        assert stale in _names(collected)

    def test_the_refresher_clones_are_never_collected(self, cache_root: Path) -> None:
        (cache_root / "refresh-souliane-skills").mkdir()

        collected = collect_unreferenced(cache_root, referenced=set(), link_dirs=[])

        # They belong to the refresher's own cadence, not to any declared pin.
        assert "refresh-souliane-skills" not in _names(collected)

    def test_an_absent_cache_root_collects_nothing_rather_than_raising(self, tmp_path: Path) -> None:
        assert collect_unreferenced(tmp_path / "nope", referenced=set(), link_dirs=[]) == []

    def test_a_file_in_the_cache_root_is_left_alone(self, cache_root: Path) -> None:
        (cache_root / "README").write_text("x", encoding="utf-8")

        collected = collect_unreferenced(cache_root, referenced=set(), link_dirs=[])

        assert "README" not in _names(collected)
        assert (cache_root / "README").is_file()
