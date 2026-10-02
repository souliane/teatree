"""``skill-metadata.json``'s Django WRITER and cold READER agree (#3829).

The skill cache is a cross-tier artifact of the #3499 / #3819 / #3826 shape.
:mod:`teatree.core.skill_cache` builds it under Django (overlay metadata, the
``requires:`` closure, the SKILL.md mtimes and the package version);
:mod:`scripts.lib.skill_loader` — stdlib-only by design, because it runs on
every UserPromptSubmit before any Django bootstrap — reads it back with its
own duplicated resolver and its own validity rules.

Every existing test sits on one side of that seam, and the failure mode is
invisible from either: a cache the reader rejects (wrong directory, a version
stamp it does not recognise, an mtime map keyed differently) degrades to the
same empty dict as a cache that is genuinely absent, so "cannot read it" and
"no skills in it" are one answer. That is #3499 verbatim.

This lane round-trips the artifact: populate through the writer, read back
through the reader, and assert the same skill set, the same ``requires:``
closure, and the same overlay metadata come out — plus the three whole-file
invalidators (directory, version stamp, mtime map) that can blank the read.
"""

import json
from pathlib import Path

import pytest

import teatree
from scripts.lib import skill_loader
from teatree.core import skill_cache
from teatree.paths import resolve_data_dir

#: A repo root that is definitionally not a worktree — the venue the prompt hook
#: runs in, and the only one whose data dir the stdlib-only reader can resolve.
_PRIMARY_CLONE = Path("/nonexistent-primary-clone")

_SKILLS = {
    "alpha": "---\nname: alpha\ndescription: Alpha skill\nrequires:\n  - beta\n---\n\n# Alpha\n",
    "beta": "---\nname: beta\ndescription: Beta skill\ncompanions:\n  - gamma\n---\n\n# Beta\n",
    "gamma": "---\nname: gamma\ndescription: Gamma skill\n---\n\n# Gamma\n",
}


@pytest.fixture
def seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put both tiers under one scratch ``HOME`` + ``XDG_DATA_HOME`` and return the skills dir.

    Those two env vars are the only knobs the stdlib-only reader has, so they
    are the only knobs set. The writer's own module constants are re-derived
    from the same environment through ITS resolver
    (:func:`teatree.paths.resolve_data_dir`) rather than from the reader's, so
    the round-trip below compares two independent answers instead of one
    answer with itself.
    """
    home = tmp_path / "home"
    skills_dir = home / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    for name, body in _SKILLS.items():
        (skills_dir / name).mkdir()
        (skills_dir / name / "SKILL.md").write_text(body, encoding="utf-8")

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    writer_data_dir = resolve_data_dir(env={"XDG_DATA_HOME": str(xdg)}, home=home, repo_root=_PRIMARY_CLONE).path
    monkeypatch.setattr(skill_cache, "DATA_DIR", writer_data_dir)
    monkeypatch.setattr(skill_cache, "_CLAUDE_SKILLS_DIR", skills_dir)
    return skills_dir


class TestSkillMetadataCacheRoundTrip:
    """What the Django writer stores is what the cold reader gets back."""

    def test_the_written_skill_set_is_the_read_skill_set(self, seam: Path) -> None:
        skill_cache.write_skill_metadata_cache()

        read_back = [entry["skill"] for entry in skill_loader._read_skill_index()]

        assert read_back == sorted(_SKILLS)

    def test_the_requires_closure_survives_the_round_trip(self, seam: Path) -> None:
        skill_cache.write_skill_metadata_cache()

        by_skill = {entry["skill"]: entry for entry in skill_loader._read_skill_index()}

        assert by_skill["alpha"]["requires"] == ["beta"]
        assert by_skill["beta"]["companions"] == ["gamma"]
        assert by_skill["gamma"]["requires"] == []

    def test_the_overlay_metadata_survives_the_round_trip(self, seam: Path) -> None:
        skill_cache.write_skill_metadata_cache()
        stored = json.loads(skill_loader.skill_metadata_cache().read_text(encoding="utf-8"))

        read_back = skill_loader.read_overlay_skill_metadata()

        assert read_back["skill_path"] == stored.get("skill_path", "")
        assert read_back["remote_patterns"] == stored.get("remote_patterns", [])

    def test_a_freshly_written_cache_is_never_rejected_as_stale(self, seam: Path) -> None:
        # The mtime map is written by one tier and validated by the other. They
        # must key it identically (skill-directory name) and read the same tree,
        # or a cache written a millisecond ago reads as stale — indistinguishable
        # from no cache at all.
        skill_cache.write_skill_metadata_cache()

        assert skill_loader._read_metadata_cache() != {}
        assert skill_loader._cache_is_stale(skill_loader._read_metadata_cache()) is False

    def test_editing_a_skill_invalidates_the_cache_in_the_reader(self, seam: Path) -> None:
        skill_cache.write_skill_metadata_cache()
        (seam / "alpha" / "SKILL.md").write_text(_SKILLS["alpha"] + "\nedited\n", encoding="utf-8")

        assert skill_loader._read_metadata_cache() == {}

    def test_a_newly_added_skill_is_picked_up_on_rewrite(self, seam: Path) -> None:
        skill_cache.write_skill_metadata_cache()
        (seam / "delta").mkdir()
        (seam / "delta" / "SKILL.md").write_text("---\nname: delta\n---\n", encoding="utf-8")

        skill_cache.write_skill_metadata_cache()

        assert [entry["skill"] for entry in skill_loader._read_skill_index()] == sorted([*_SKILLS, "delta"])


class TestSkillMetadataCacheLocationParity:
    """Both tiers resolve the cache file from the same environment."""

    def test_the_reader_finds_the_file_the_writer_wrote(self, seam: Path, tmp_path: Path) -> None:
        skill_cache.write_skill_metadata_cache()

        assert (tmp_path / "xdg" / "teatree" / "skill-metadata.json").is_file()
        assert skill_loader.skill_metadata_cache() == skill_cache.DATA_DIR / "skill-metadata.json"

    @pytest.mark.parametrize("explicit_xdg", [None, "sandbox"])
    def test_the_cold_data_dir_tracks_the_writers_resolver(
        self, explicit_xdg: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``resolve_data_dir`` is the writer's answer. For a primary clone — the
        # only venue the prompt hook runs in — the cold duplicate must agree with
        # it under every environment, not just the default one.
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        env: dict[str, str] = {}
        if explicit_xdg is None:
            monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        else:
            env["XDG_DATA_HOME"] = str(tmp_path / explicit_xdg)
            monkeypatch.setenv("XDG_DATA_HOME", env["XDG_DATA_HOME"])

        expected = resolve_data_dir(env=env, home=home, repo_root=_PRIMARY_CLONE).path

        assert skill_loader.xdg_data_dir() == expected


class TestSkillMetadataVersionStampParity:
    """The stamp the writer records is the stamp the reader compares against."""

    def test_the_writer_stamps_the_version_the_reader_checks(self, seam: Path) -> None:
        # The writer stamps ``teatree.__version__``; the reader compares the
        # installed distribution version. A bump to one alone silently blanks
        # every cached read — the reader would report "no skills found" forever.
        skill_cache.write_skill_metadata_cache()
        stored = json.loads(skill_loader.skill_metadata_cache().read_text(encoding="utf-8"))

        assert stored["teatree_version"] == teatree.__version__
        assert stored["teatree_version"] == skill_loader._get_installed_version()

    def test_a_foreign_version_stamp_is_rejected_by_the_reader(self, seam: Path) -> None:
        skill_cache.write_skill_metadata_cache()
        cache = skill_loader.skill_metadata_cache()
        stored = json.loads(cache.read_text(encoding="utf-8"))
        stored["teatree_version"] = "0.0.0-not-this-build"
        cache.write_text(json.dumps(stored), encoding="utf-8")

        assert skill_loader._read_metadata_cache() == {}
