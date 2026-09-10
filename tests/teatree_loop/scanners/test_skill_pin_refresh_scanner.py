"""The refresher fetches FIRST-PARTY sources only, and signals only a real gap (#4677).

The allowlist is load-bearing rather than a detail: a blanket "bump every pin" pass
would pull unreviewed third-party skill code onto the box on a timer, which is exactly
what pinning exists to prevent. A third-party fixture that IS ahead of its pin is
carried here for that reason — the test fails the moment the allowlist widens to cover
it.
"""

from pathlib import Path

import pytest

from teatree.loop.scanners.skill_pin_refresh import SKILL_PIN_BEHIND_KIND, SkillPinRefreshScanner
from tests._git_repo import make_git_repo, run_git

_FIRST_PARTY = "souliane/skills"
_THIRD_PARTY = "obra/superpowers"


def _publish(root: Path, slug: str) -> Path:
    origin = make_git_repo(root / slug)
    (origin / "skills" / "a-skill").mkdir(parents=True)
    (origin / "skills" / "a-skill" / "SKILL.md").write_text("---\n", encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "publish")
    return origin


def _move_on(origin: Path, subject: str = "move on") -> str:
    (origin / "skills" / "a-skill" / "SKILL.md").write_text(f"---\n{subject}\n", encoding="utf-8")
    run_git(origin, "commit", "-qam", subject)
    return run_git(origin, "rev-parse", "HEAD")


@pytest.fixture
def remotes(tmp_path: Path) -> Path:
    return tmp_path / "remotes"


def _repo_with(tmp_path: Path, *specs: str) -> Path:
    repo = tmp_path / "teatree"
    repo.mkdir(parents=True, exist_ok=True)
    entries = "".join(f"  - {spec}\n" for spec in specs)
    (repo / "apm.yml").write_text(f"name: souliane/teatree\ndependencies:\n  apm:\n{entries}", encoding="utf-8")
    return repo


def _scan(tmp_path: Path, repo: Path) -> list:
    scanner = SkillPinRefreshScanner(repo=repo, cache_root=tmp_path / "cache", remote_base=f"{tmp_path / 'remotes'}/")
    return scanner.scan()


class TestFirstPartyDriftIsSignalled:
    def test_a_first_party_source_ahead_of_its_pin_emits_one_signal(self, tmp_path: Path, remotes: Path) -> None:
        origin = _publish(remotes, _FIRST_PARTY)
        pinned = run_git(origin, "rev-parse", "HEAD")
        head = _move_on(origin)
        repo = _repo_with(tmp_path, f"{_FIRST_PARTY}/ac-python#{pinned}")

        [signal] = _scan(tmp_path, repo)

        assert signal.kind == SKILL_PIN_BEHIND_KIND
        assert signal.payload["source"] == _FIRST_PARTY
        assert signal.payload["head"] == head
        assert signal.payload["pinned"] == pinned

    def test_every_spec_sharing_the_source_rides_one_signal(self, tmp_path: Path, remotes: Path) -> None:
        origin = _publish(remotes, _FIRST_PARTY)
        pinned = run_git(origin, "rev-parse", "HEAD")
        _move_on(origin)
        repo = _repo_with(tmp_path, f"{_FIRST_PARTY}/ac-python#{pinned}", f"{_FIRST_PARTY}/ac-django#{pinned}")

        [signal] = _scan(tmp_path, repo)

        assert sorted(signal.payload["specs"]) == [
            f"{_FIRST_PARTY}/ac-django#{pinned}",
            f"{_FIRST_PARTY}/ac-python#{pinned}",
        ]

    def test_the_signal_carries_what_the_bump_would_bring_in(self, tmp_path: Path, remotes: Path) -> None:
        origin = _publish(remotes, _FIRST_PARTY)
        pinned = run_git(origin, "rev-parse", "HEAD")
        _move_on(origin, "fix the skill")
        repo = _repo_with(tmp_path, f"{_FIRST_PARTY}/ac-python#{pinned}")

        [signal] = _scan(tmp_path, repo)

        assert "fix the skill" in signal.payload["log"]


class TestTheAllowlistIsLoadBearing:
    def test_a_third_party_source_ahead_of_its_pin_is_never_signalled(self, tmp_path: Path, remotes: Path) -> None:
        origin = _publish(remotes, _THIRD_PARTY)
        pinned = run_git(origin, "rev-parse", "HEAD")
        _move_on(origin)
        repo = _repo_with(tmp_path, f"{_THIRD_PARTY}#{pinned}")

        assert _scan(tmp_path, repo) == []

    def test_a_third_party_source_is_never_even_fetched(self, tmp_path: Path, remotes: Path) -> None:
        origin = _publish(remotes, _THIRD_PARTY)
        pinned = run_git(origin, "rev-parse", "HEAD")
        _move_on(origin)
        repo = _repo_with(tmp_path, f"{_THIRD_PARTY}#{pinned}")

        _scan(tmp_path, repo)

        # Not fetching is the guarantee: unreviewed third-party code never lands
        # on the box on a timer, whatever a later reader does with the signal.
        assert not (tmp_path / "cache" / "obra-superpowers").exists()

    def test_a_first_party_source_beside_a_third_party_one_is_still_signalled(
        self, tmp_path: Path, remotes: Path
    ) -> None:
        first = _publish(remotes, _FIRST_PARTY)
        third = _publish(remotes, _THIRD_PARTY)
        first_pin = run_git(first, "rev-parse", "HEAD")
        third_pin = run_git(third, "rev-parse", "HEAD")
        _move_on(first)
        _move_on(third)
        repo = _repo_with(tmp_path, f"{_FIRST_PARTY}/ac-python#{first_pin}", f"{_THIRD_PARTY}#{third_pin}")

        signals = _scan(tmp_path, repo)

        assert [signal.payload["source"] for signal in signals] == [_FIRST_PARTY]


class TestTheScannerIsNotVacuous:
    def test_a_source_already_at_its_pin_signals_nothing(self, tmp_path: Path, remotes: Path) -> None:
        origin = _publish(remotes, _FIRST_PARTY)
        at_head = run_git(origin, "rev-parse", "HEAD")
        repo = _repo_with(tmp_path, f"{_FIRST_PARTY}/ac-python#{at_head}")

        assert _scan(tmp_path, repo) == []

    def test_an_unreachable_source_signals_nothing_rather_than_guessing(self, tmp_path: Path) -> None:
        repo = _repo_with(tmp_path, f"{_FIRST_PARTY}/ac-python#abc1234")

        assert _scan(tmp_path, repo) == []

    def test_a_symbolic_pin_has_no_sha_to_bump(self, tmp_path: Path, remotes: Path) -> None:
        origin = _publish(remotes, _FIRST_PARTY)
        _move_on(origin)
        repo = _repo_with(tmp_path, f"{_FIRST_PARTY}/ac-python#main")

        assert _scan(tmp_path, repo) == []

    def test_a_checkout_with_no_manifest_signals_nothing(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        assert SkillPinRefreshScanner(repo=empty, cache_root=tmp_path / "cache").scan() == []
