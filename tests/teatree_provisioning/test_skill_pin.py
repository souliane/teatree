"""Pin-vs-head comparison — the measurement behind the bump suggestion.

The sibling drift measurement answers "did my INSTALL leave the pin"; this one
answers "did the PIN leave its source", which nothing else asks. These exercise
the real thing: a real git repo as the source, a real ``git ls-remote`` against
it, and the mandate read from a real ``apm.yml`` — including the property the
whole check exists for, that a source it cannot reach is reported as unknown
rather than as agreement.
"""

import datetime as dt
from pathlib import Path

import pytest

from teatree.provisioning.declared import bundles_declared_in_apm_manifest, skills_declared_in_apm_manifest
from teatree.provisioning.skill_pin import (
    PinAudit,
    SkillPinStatus,
    carry_behind_since,
    first_party_owner,
    measure_skill_pins,
    pin_advisory_lines,
    read_pin_audit,
    write_pin_audit,
)
from tests._git_repo import make_git_repo, run_git

_SKILL = "ac-python"
_OWNER_REPO = "team/skills"


def _write_manifest(root: Path, entries: list[str]) -> Path:
    """A minimal ``apm.yml`` declaring *entries* on the mandated-skill surface."""
    manifest = root / "apm.yml"
    body = "\n".join(f"  - {entry}" for entry in entries)
    manifest.write_text(f"name: team/thing\ndependencies:\n  apm:\n{body}\n", encoding="utf-8")
    return manifest


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A source repo published at ``<remote base>team/skills``, one commit deep."""
    origin = make_git_repo(tmp_path / _OWNER_REPO)
    (origin / _SKILL).mkdir()
    (origin / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\n", encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "publish the skill")
    return origin


def _remote_base(tmp_path: Path) -> str:
    """``measure_skill_pins`` joins this to ``<owner>/<repo>`` — a local dir here."""
    return f"{tmp_path}/"


def _measure(tmp_path: Path, pinned_at: str, *, base: str | None = None) -> list:
    manifest = _write_manifest(tmp_path, [f"{_OWNER_REPO}/{_SKILL}#{pinned_at}"])
    declared = skills_declared_in_apm_manifest(manifest)
    return measure_skill_pins(declared, remote_base=_remote_base(tmp_path) if base is None else base)


class TestMeasureSkillPins:
    def test_pin_at_the_source_head_suggests_nothing(self, tmp_path: Path, source: Path) -> None:
        head = run_git(source, "rev-parse", "HEAD")
        [status] = _measure(tmp_path, head)
        assert status.is_current
        assert not status.is_behind
        assert status.unmeasurable == ""
        assert pin_advisory_lines([status]) == []

    def test_pin_the_source_moved_past_names_the_sha_to_bump_to(self, tmp_path: Path, source: Path) -> None:
        # The defect: work merged after the pin was written is unreachable by
        # every consumer, and nothing anywhere is red about it.
        pinned = run_git(source, "rev-parse", "HEAD")
        (source / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\nfixed\n", encoding="utf-8")
        run_git(source, "commit", "-qam", "fix the skill")
        moved_to = run_git(source, "rev-parse", "HEAD")

        [status] = _measure(tmp_path, pinned)

        assert status.is_behind
        assert not status.is_current
        assert status.head_sha == moved_to
        assert status.bumped_spec == f"{_OWNER_REPO}/{_SKILL}#{moved_to}"
        [line] = pin_advisory_lines([status])
        assert line.startswith("INFO")
        assert moved_to in line
        # Pasteable: the fix is a runnable command carrying the NEW sha.
        assert f"apm install {_OWNER_REPO}/{_SKILL}#{moved_to}" in line

    def test_unreachable_source_is_unmeasurable_never_up_to_date(self, tmp_path: Path) -> None:
        [status] = _measure(tmp_path, "d0008a3", base=f"{tmp_path / 'nowhere'}/")
        assert status.unmeasurable
        assert not status.is_current
        assert not status.is_behind
        [line] = pin_advisory_lines([status])
        assert line.startswith("WARN")
        assert "UNVERIFIED" in line
        assert "up to date" not in line.lower()

    def test_entry_carrying_no_pin_has_no_pin_to_measure(self, tmp_path: Path, source: Path) -> None:
        manifest = _write_manifest(tmp_path, [f"{_OWNER_REPO}/{_SKILL}"])
        declared = skills_declared_in_apm_manifest(manifest)
        assert measure_skill_pins(declared, remote_base=_remote_base(tmp_path)) == []


class TestSymbolicPinsAreNotShaPins:
    """A pin naming a moving ref cannot "fall behind" the ref it names (#4193).

    ``is_current`` compared the pin to the head sha as strings, so ``#main`` never
    matched and every symbolic pin read as permanently behind — with a remediation
    telling the operator to replace their deliberately floating ref with a frozen sha,
    reversing the choice the declaration was making. Symbolic and immutable pins are
    different kinds and are answered differently.
    """

    def test_a_pin_on_the_default_branch_is_current_and_suggests_nothing(self, tmp_path: Path, source: Path) -> None:
        branch = run_git(source, "rev-parse", "--abbrev-ref", "HEAD")

        [status] = _measure(tmp_path, branch)

        assert status.is_symbolic
        assert status.is_current
        assert not status.is_behind
        assert pin_advisory_lines([status]) == []

    def test_a_moved_source_does_not_make_a_default_branch_pin_behind(self, tmp_path: Path, source: Path) -> None:
        """The regression proper: the source advancing is exactly what a floating pin tracks."""
        branch = run_git(source, "rev-parse", "--abbrev-ref", "HEAD")
        (source / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\nmoved on\n", encoding="utf-8")
        run_git(source, "commit", "-qam", "advance the source")

        [status] = _measure(tmp_path, branch)

        assert not status.is_behind
        assert status.is_current
        assert pin_advisory_lines([status]) == []

    def test_a_symbolic_ref_that_was_never_compared_is_unverified(self, tmp_path: Path, source: Path) -> None:
        """The probe reads HEAD, so a pin naming some other ref was compared to nothing."""
        [status] = _measure(tmp_path, "v5.0.7")

        assert status.is_symbolic
        assert status.is_unverified_ref
        assert not status.is_current
        assert not status.is_behind
        [line] = pin_advisory_lines([status])
        assert line.startswith("WARN")
        assert "UNVERIFIED" in line
        assert "apm install" not in line  # never advise freezing a ref that was not measured

    def test_an_abbreviated_sha_pin_at_head_is_current(self, tmp_path: Path, source: Path) -> None:
        """``#d0008a3`` and its full sha name the same commit — a prefix is not "behind"."""
        head = run_git(source, "rev-parse", "HEAD")

        [status] = _measure(tmp_path, head[:12])

        assert not status.is_symbolic
        assert status.is_current
        assert not status.is_behind

    def test_a_sha_pin_the_source_moved_past_is_still_behind(self, tmp_path: Path, source: Path) -> None:
        """The teeth: relaxing the comparison must not stop a real immutable pin trailing."""
        pinned = run_git(source, "rev-parse", "HEAD")
        (source / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\nmoved on\n", encoding="utf-8")
        run_git(source, "commit", "-qam", "advance the source")

        [status] = _measure(tmp_path, pinned)

        assert status.is_behind
        assert not status.is_current


class TestFirstPartyOwner:
    """Which pins are the owner's OWN is read from the manifest, not an allowlist (#4677)."""

    def test_the_owner_is_read_from_the_manifests_own_name(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path, [f"{_OWNER_REPO}/{_SKILL}#d0008a3"])

        assert first_party_owner(manifest) == "team"

    def test_a_manifest_naming_no_owner_classifies_nothing_as_first_party(self, tmp_path: Path) -> None:
        manifest = tmp_path / "apm.yml"
        manifest.write_text("dependencies:\n  apm:\n  - a/b#c\n", encoding="utf-8")

        assert first_party_owner(manifest) == ""


class TestFirstPartyPinsAreDriftNotAHold:
    """A first-party pin trailing its source is drift; a third-party one may be held (#4677)."""

    def _behind(self, tmp_path: Path, source: Path, *, owner: str) -> list:
        pinned = run_git(source, "rev-parse", "HEAD")
        (source / _SKILL / "SKILL.md").write_text("---\nname: ac-python\n---\nfixed\n", encoding="utf-8")
        run_git(source, "commit", "-qam", "fix the skill")
        manifest = _write_manifest(tmp_path, [f"{_OWNER_REPO}/{_SKILL}#{pinned}"])
        declared = skills_declared_in_apm_manifest(manifest)
        return measure_skill_pins(declared, remote_base=_remote_base(tmp_path), first_party_owner=owner)

    def test_a_first_party_pin_behind_its_source_is_a_warn_not_an_info(self, tmp_path: Path, source: Path) -> None:
        [status] = self._behind(tmp_path, source, owner="team")

        assert status.first_party
        [line] = pin_advisory_lines([status])
        assert line.startswith("WARN")

    def test_the_held_deliberately_caveat_is_not_offered_for_a_first_party_pin(
        self, tmp_path: Path, source: Path
    ) -> None:
        [status] = self._behind(tmp_path, source, owner="team")

        [line] = pin_advisory_lines([status])
        assert "held deliberately" not in line
        assert "drift" in line

    def test_a_third_party_pin_keeps_the_info_and_its_caveat(self, tmp_path: Path, source: Path) -> None:
        [status] = self._behind(tmp_path, source, owner="somebody-else")

        assert not status.first_party
        [line] = pin_advisory_lines([status])
        assert line.startswith("INFO")
        assert "held deliberately" in line

    def test_an_unclassified_measurement_stays_third_party(self, tmp_path: Path, source: Path) -> None:
        # No owner passed: nothing may be promoted to first-party by accident.
        [status] = self._behind(tmp_path, source, owner="")

        assert not status.first_party


class TestBundlePinsAreMeasured:
    """The manifest's only third-party pin is a bundle, and it is measured (#4677)."""

    @pytest.fixture
    def bundle(self, tmp_path: Path) -> Path:
        origin = make_git_repo(tmp_path / "obra" / "superpowers")
        (origin / "skills" / "writing-plans").mkdir(parents=True)
        (origin / "skills" / "writing-plans" / "SKILL.md").write_text("---\n", encoding="utf-8")
        run_git(origin, "add", "-A")
        run_git(origin, "commit", "-q", "-m", "publish")
        return origin

    def test_a_whole_repo_bundle_pin_is_compared_against_its_source(self, tmp_path: Path, bundle: Path) -> None:
        pinned = run_git(bundle, "rev-parse", "HEAD")
        (bundle / "skills" / "writing-plans" / "SKILL.md").write_text("---\nx\n", encoding="utf-8")
        run_git(bundle, "commit", "-qam", "move on")
        manifest = _write_manifest(tmp_path, [f"obra/superpowers#{pinned}"])

        [status] = measure_skill_pins(bundles_declared_in_apm_manifest(manifest), remote_base=_remote_base(tmp_path))

        assert status.is_behind
        assert status.spec == f"obra/superpowers#{pinned}"


class TestBehindSinceIsCarriedForward:
    """How LONG a pin has been behind is the fact one measurement cannot hold (#4677)."""

    _SPEC = "team/skills/ac-python#aaaaaaa"

    def _behind_status(self) -> SkillPinStatus:
        return SkillPinStatus(
            name="ac-python", spec=self._SPEC, pinned_ref="aaaaaaa", head_sha="b" * 40, branch="main", first_party=True
        )

    def _current_status(self) -> SkillPinStatus:
        return SkillPinStatus(
            name="ac-python", spec=self._SPEC, pinned_ref="b" * 40, head_sha="b" * 40, branch="main", first_party=True
        )

    def test_a_newly_behind_pin_is_stamped_with_the_moment_it_was_seen(self) -> None:
        first_seen = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

        carried = carry_behind_since([self._behind_status()], previous=None, now=first_seen)

        assert carried == {self._SPEC: first_seen}

    def test_a_still_behind_pin_keeps_its_original_timestamp(self) -> None:
        first_seen = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
        previous = PinAudit(measured_at=first_seen, behind_since={self._SPEC: first_seen})

        carried = carry_behind_since(
            [self._behind_status()], previous=previous, now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        )

        # Re-stamping on every setup run would reset the age forever, so a pin could
        # never mature past the FAIL threshold however long it trailed.
        assert carried == {self._SPEC: first_seen}

    def test_a_pin_that_caught_up_is_dropped_rather_than_kept_stale(self) -> None:
        first_seen = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
        previous = PinAudit(measured_at=first_seen, behind_since={self._SPEC: first_seen})

        carried = carry_behind_since(
            [self._current_status()], previous=previous, now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        )

        assert carried == {}

    def test_the_stamp_round_trips_through_the_recorded_measurement(self, tmp_path: Path) -> None:
        first_seen = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
        record = tmp_path / "audit.json"
        write_pin_audit(
            PinAudit(
                measured_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
                statuses=(self._behind_status(),),
                behind_since={self._SPEC: first_seen},
            ),
            record,
        )

        audit = read_pin_audit(record)

        assert audit is not None
        assert audit.behind_since == {self._SPEC: first_seen}
        assert audit.days_behind(self._SPEC, now=dt.datetime(2026, 9, 15, tzinfo=dt.UTC)) == 45

    def test_a_record_written_before_the_stamp_existed_still_reads(self, tmp_path: Path) -> None:
        record = tmp_path / "audit.json"
        record.write_text('{"measured_at": "2026-08-01T00:00:00+00:00", "statuses": []}', encoding="utf-8")

        audit = read_pin_audit(record)

        assert audit is not None
        assert audit.behind_since == {}
