"""What a remote already holds is read from the remote itself, at the URL a push goes to, never from a tracking ref."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.hooks.remote_holds import remote_holdings


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        [str(shutil.which("git")), *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path, str]:
    """``(origin, work, tip)``: a work clone of an origin whose only branch is at ``tip``."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    tip = _commit(origin, "base")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    return origin, work, tip


class TestWhatTheRemoteListsIsWhatItHolds:
    def test_every_head_the_remote_lists_that_this_clone_has(self, clone: tuple[Path, Path, str]) -> None:
        origin, work, tip = clone
        _git(origin, "checkout", "-q", "-b", "other")
        other = _commit(origin, "other")
        _git(work, "fetch", "-q", "origin")

        holdings = remote_holdings(work, remote_url=str(origin), remote_name="origin")

        assert holdings.listed
        assert set(holdings.tips) == {tip, other}

    def test_a_head_whose_commit_this_clone_never_fetched_is_skipped(self, clone: tuple[Path, Path, str]) -> None:
        origin, work, tip = clone
        _git(origin, "checkout", "-q", "-b", "teammate")
        _commit(origin, "theirs")

        holdings = remote_holdings(work, remote_url=str(origin), remote_name="origin")

        assert holdings.listed
        assert holdings.tips == (tip,)

    def test_the_sha_git_reported_for_the_ref_is_added(self, clone: tuple[Path, Path, str]) -> None:
        origin, work, tip = clone
        local = _commit(work, "local")
        _git(work, "branch", "feature")

        holdings = remote_holdings(work, remote_url=str(origin), remote_name="origin", reported_shas=[local])

        assert set(holdings.tips) == {tip, local}

    def test_a_planted_tracking_ref_adds_nothing(self, clone: tuple[Path, Path, str]) -> None:
        origin, work, tip = clone
        leak = _commit(work, "leak")
        _git(work, "update-ref", "refs/remotes/origin/planted", leak)

        holdings = remote_holdings(work, remote_url=str(origin), remote_name="origin")

        assert holdings.tips == (tip,)


class TestARemoteThatCannotBeListedHoldsOnlyWhatGitReported:
    def test_an_unreachable_remote(self, clone: tuple[Path, Path, str], tmp_path: Path) -> None:
        _origin, work, tip = clone
        gone = tmp_path / "no-such-repository"

        holdings = remote_holdings(work, remote_url=str(gone), remote_name="origin", reported_shas=[tip])

        assert not holdings.listed
        assert holdings.tips == (tip,)
        assert "could not list" in holdings.reason

    def test_nothing_is_held_when_nothing_was_reported(self, clone: tuple[Path, Path, str], tmp_path: Path) -> None:
        _origin, work, _tip = clone

        holdings = remote_holdings(work, remote_url=str(tmp_path / "no-such-repository"), remote_name="origin")

        assert not holdings.listed
        assert holdings.tips == ()


class TestAUrlRewriteCannotSendTheListingElsewhere:
    """``url.<x>.insteadOf`` at a fake repository plus an identity ``pushInsteadOf`` keeps the push on the real one."""

    def _split(self, origin: Path, work: Path, fake: Path) -> None:
        _git(work, "config", f"url.{fake}.insteadOf", str(origin))
        _git(work, "config", f"url.{origin}.pushInsteadOf", str(origin))

    def test_the_listing_would_go_to_the_fake_repository(self, clone: tuple[Path, Path, str], tmp_path: Path) -> None:
        origin, work, _tip = clone
        leak = _commit(work, "leak")
        fake = tmp_path / "fake"
        _git(tmp_path, "clone", "-q", str(work), str(fake))
        self._split(origin, work, fake)

        holdings = remote_holdings(work, remote_url=str(origin), remote_name="origin")

        assert not holdings.listed, "the push goes to the real remote while the listing would go to the fake one"
        assert leak not in holdings.tips
        assert "insteadOf" in holdings.reason

    def test_the_sha_git_reported_still_counts(self, clone: tuple[Path, Path, str], tmp_path: Path) -> None:
        origin, work, tip = clone
        fake = tmp_path / "fake"
        _git(tmp_path, "clone", "-q", str(work), str(fake))
        self._split(origin, work, fake)

        holdings = remote_holdings(work, remote_url=str(origin), remote_name="origin", reported_shas=[tip])

        assert holdings.tips == (tip,)

    def test_a_push_url_other_than_the_one_git_passed_is_not_listed(
        self, clone: tuple[Path, Path, str], tmp_path: Path
    ) -> None:
        origin, work, _tip = clone
        elsewhere = tmp_path / "elsewhere"
        _git(tmp_path, "clone", "-q", "--bare", str(origin), str(elsewhere))
        _git(work, "remote", "set-url", "--push", "origin", str(elsewhere))

        holdings = remote_holdings(work, remote_url=str(origin), remote_name="origin")

        assert not holdings.listed
        assert "differs" in holdings.reason
        assert "insteadOf" not in holdings.reason

    def test_a_remote_name_git_does_not_know_is_not_blamed_on_a_rewrite(self, clone: tuple[Path, Path, str]) -> None:
        """A push to a raw URL has no remote of that name; the reason says the push URL could not be confirmed."""
        origin, work, _tip = clone

        holdings = remote_holdings(work, remote_url=str(origin), remote_name=str(origin))

        assert not holdings.listed
        assert "could not confirm the URL git pushes to" in holdings.reason
        assert "insteadOf" not in holdings.reason

    def test_an_unrewritten_url_is_listed(self, clone: tuple[Path, Path, str]) -> None:
        origin, work, _tip = clone

        assert remote_holdings(work, remote_url=str(origin), remote_name="origin").listed


class TestTheCommandLine:
    def _run(self, work: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        return subprocess.run(
            [sys.executable, "-m", "teatree.hooks.remote_holds", *args],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_it_prints_one_tip_per_line(self, clone: tuple[Path, Path, str]) -> None:
        origin, work, tip = clone

        result = self._run(work, str(origin), "origin")

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [tip]
        assert result.stderr == ""

    def test_it_says_so_on_stderr_when_the_remote_could_not_be_listed(
        self, clone: tuple[Path, Path, str], tmp_path: Path
    ) -> None:
        _origin, work, tip = clone

        result = self._run(work, str(tmp_path / "no-such-repository"), "origin", tip)

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == [tip]
        assert "could not list" in result.stderr.lower()
        assert "wider" in result.stderr
