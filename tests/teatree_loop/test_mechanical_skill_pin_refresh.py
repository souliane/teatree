"""The bump PR is opened once per head, against a real git origin (#4677).

The refresher must not mutate the running host: `apm.yml` stays the single source of
truth, the change lands as a reviewable PR, and `t3 setup` installs it on its next run.
Idempotence is on the HEAD, not on the tick — a condition that persists across ticks
opens one PR, and a source that moves again opens another.
"""

from pathlib import Path

import pytest

from teatree.loop.mechanical_skill_pin_refresh import SkillPinBumper
from tests._git_repo import make_git_repo, run_git

_PINNED = "a" * 40
_HEAD = "b" * 40
_MANIFEST = (
    "name: souliane/teatree\ndependencies:\n  apm:\n"
    "  - obra/superpowers#1f20bef  # v5.0.7\n"
    f"  - souliane/skills/ac-python#{_PINNED}\n"
    f"  - souliane/skills/ac-django#{_PINNED}\n"
)


class FakeForge:
    """Records what would be published, and can report a PR already open."""

    def __init__(self, existing: str = "") -> None:
        self.existing = existing
        self.created: list[dict[str, str]] = []

    def find_pr_url(self, *, branch: str) -> str:
        return self.existing

    def create_pr(self, *, branch: str, title: str, body: str) -> str:
        self.created.append({"branch": branch, "title": title, "body": body})
        return f"https://example.invalid/pr/{len(self.created)}"

    def update_pr(self, *, url: str, body: str) -> None:  # pragma: no cover — unused by the bumper
        raise NotImplementedError


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone whose `origin` is a real bare repo, so a push is really a push."""
    origin = make_git_repo(tmp_path / "origin")
    (origin / "apm.yml").write_text(_MANIFEST, encoding="utf-8")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "declare the pins")

    clone = tmp_path / "clone"
    run_git(origin, "clone", "-q", str(origin), str(clone))
    # A real teatree checkout carries a commit identity (`t3 setup` installs one);
    # the fixture must too, or the bumper's commit fails for a reason production
    # never has.
    run_git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    run_git(clone, "config", "user.name", "t")
    return clone


def _payload(**overrides: object) -> dict:
    payload = {
        "repo": "",
        "source": "souliane/skills",
        "pinned": _PINNED,
        "head": _HEAD,
        "specs": [f"souliane/skills/ac-python#{_PINNED}", f"souliane/skills/ac-django#{_PINNED}"],
        "bumped_specs": [f"souliane/skills/ac-python#{_HEAD}", f"souliane/skills/ac-django#{_HEAD}"],
        "log": "b0b0b0b fix(ac-python): target Python 3.14",
    }
    return payload | overrides


def _bump(repo: Path, forge: FakeForge, **overrides: object) -> object:
    return SkillPinBumper(repo=repo, forge=forge).bump(_payload(**overrides))


class TestTheBumpLandsAsAReviewablePr:
    def test_a_pr_is_opened_for_the_new_head(self, repo: Path) -> None:
        forge = FakeForge()

        result = _bump(repo, forge)

        assert result.pr_url
        assert len(forge.created) == 1

    def test_the_branch_carries_the_source_and_the_head(self, repo: Path) -> None:
        forge = FakeForge()

        _bump(repo, forge)

        assert forge.created[0]["branch"] == f"skill-pin-refresh/souliane-skills-{_HEAD[:7]}"

    def test_every_spec_of_that_source_is_rewritten_to_the_new_head(self, repo: Path) -> None:
        _bump(repo, FakeForge())

        pushed = run_git(repo, "show", f"skill-pin-refresh/souliane-skills-{_HEAD[:7]}:apm.yml")

        assert f"souliane/skills/ac-python#{_HEAD}" in pushed
        assert f"souliane/skills/ac-django#{_HEAD}" in pushed
        assert _PINNED not in pushed

    def test_a_third_party_pin_is_left_exactly_as_declared(self, repo: Path) -> None:
        _bump(repo, FakeForge())

        pushed = run_git(repo, "show", f"skill-pin-refresh/souliane-skills-{_HEAD[:7]}:apm.yml")

        # Comment and sha both intact: the refresher rewrites its own source only.
        assert "  - obra/superpowers#1f20bef  # v5.0.7" in pushed

    def test_the_body_says_what_the_bump_brings_in(self, repo: Path) -> None:
        forge = FakeForge()

        _bump(repo, forge)

        assert "fix(ac-python): target Python 3.14" in forge.created[0]["body"]

    def test_the_running_host_is_not_mutated(self, repo: Path) -> None:
        _bump(repo, FakeForge())

        # The clone's own checkout still declares the OLD pin — only the branch moved.
        assert _PINNED in (repo / "apm.yml").read_text(encoding="utf-8")


class TestIdempotenceOnTheHead:
    def test_a_second_pass_over_the_same_head_opens_no_second_pr(self, repo: Path) -> None:
        forge = FakeForge()

        _bump(repo, forge)
        second = _bump(repo, forge)

        assert len(forge.created) == 1
        assert second.skipped

    def test_an_already_open_pr_is_left_alone(self, repo: Path) -> None:
        forge = FakeForge(existing="https://example.invalid/pr/already")

        result = _bump(repo, forge)

        assert forge.created == []
        assert result.skipped

    def test_a_source_that_moved_again_gets_its_own_pr(self, repo: Path) -> None:
        forge = FakeForge()
        moved_again = "c" * 40

        _bump(repo, forge)
        _bump(
            repo,
            forge,
            head=moved_again,
            specs=[f"souliane/skills/ac-python#{_PINNED}"],
            bumped_specs=[f"souliane/skills/ac-python#{moved_again}"],
        )

        assert len(forge.created) == 2


class TestFailuresAreReportedNotRaised:
    def test_a_payload_naming_no_spec_changes_nothing(self, repo: Path) -> None:
        forge = FakeForge()

        result = _bump(repo, forge, specs=[], bumped_specs=[])

        assert forge.created == []
        assert result.skipped

    def test_a_spec_the_manifest_does_not_carry_opens_no_pr(self, repo: Path) -> None:
        forge = FakeForge()

        result = _bump(
            repo, forge, specs=["souliane/skills/ac-rust#" + _PINNED], bumped_specs=["souliane/skills/ac-rust#" + _HEAD]
        )

        assert forge.created == []
        assert result.skipped
