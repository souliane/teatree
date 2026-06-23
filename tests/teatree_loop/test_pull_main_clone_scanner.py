"""Tests for :class:`PullMainCloneScanner` — pull work-repo main clones per tick.

After a ticket/MR/PR merges, the corresponding work-repo *main clone*
under ``$T3_WORKSPACE_DIR`` drifts behind ``origin/<default-branch>``.
A stale main clone silently poisons investigations — ``git show`` /
``grep`` against a clone parked one merge behind (or, worse, on a
leftover feature branch) returns wrong answers. This scanner closes the
loop: every tick (subject to the cadence gate) it fast-forwards each
configured work-repo main clone to its tracking branch.

"A merge happened since last tick" is detected the robust way: a merge
is precisely the event that advances ``origin/<default>``. The scanner
``git fetch``es, and if the local default-branch HEAD now trails the
remote it ff-pulls. An already-current clone is a no-op (``up_to_date``)
that emits no ``updated`` signal — idempotent, no spam.

Safety is the whole point: the scanner only ever ``pull --ff-only``. A
dirty working tree, a non-default-branch checkout, or a non-fast-forward
remote is a *skip with a reason*, never a reset / force / stash. Real
``git`` runs against ``tmp_path`` clones (no mocks) so the assertions
pin the actual git invocations and the SHAs observed afterwards.
"""

import datetime as _dt
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.pull_main_clone_marker import PullMainCloneMarker
from teatree.loop.scanners.pull_main_clone import PullMainCloneScanner

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Thin ``subprocess.run`` wrapper — capture text output and require exit 0."""
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_env() -> dict[str, str]:
    """Deterministic git env that lets ``commit`` succeed in a CI sandbox."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    return env


def _seed_origin_with_two_commits(origin_dir: Path) -> None:
    """Create a bare repo with two commits on ``main`` for ff-pull testing."""
    env = _git_env()
    seed = origin_dir.parent / f"_seed_{origin_dir.name}"
    seed.mkdir()
    _run("git", "init", "--initial-branch=main", str(seed), cwd=origin_dir.parent, env=env)
    (seed / "a.txt").write_text("a")
    _run("git", "add", "a.txt", cwd=seed, env=env)
    _run("git", "commit", "-m", "first", cwd=seed, env=env)
    (seed / "b.txt").write_text("b")
    _run("git", "add", "b.txt", cwd=seed, env=env)
    _run("git", "commit", "-m", "second", cwd=seed, env=env)
    _run("git", "init", "--bare", "--initial-branch=main", str(origin_dir), cwd=origin_dir.parent, env=env)
    _run("git", "remote", "add", "origin", str(origin_dir), cwd=seed, env=env)
    _run("git", "push", "-u", "origin", "main", cwd=seed, env=env)


def _clone_trailing_by_one_commit(*, origin: Path, clone: Path) -> str:
    """Clone *origin*, reset HEAD back one commit so a ff-pull is needed.

    Models the post-merge state of a work-repo main clone: ``origin/main``
    advanced (the merge), the local clone has not pulled yet. Returns the
    SHA the clone is checked out on (one behind ``origin/main``).
    """
    env = _git_env()
    _run("git", "clone", "--no-local", str(origin), str(clone), cwd=clone.parent, env=env)
    _run("git", "remote", "set-head", "origin", "main", cwd=clone, env=env)
    _run("git", "reset", "--hard", "HEAD~1", cwd=clone, env=env)
    return _run("git", "rev-parse", "HEAD", cwd=clone, env=env).stdout.strip()


def _clone_up_to_date(*, origin: Path, clone: Path) -> str:
    """Clone *origin* with no trailing commit — already up to date."""
    env = _git_env()
    _run("git", "clone", "--no-local", str(origin), str(clone), cwd=clone.parent, env=env)
    _run("git", "remote", "set-head", "origin", "main", cwd=clone, env=env)
    return _run("git", "rev-parse", "HEAD", cwd=clone, env=env).stdout.strip()


def _checkout_feature_branch(clone: Path) -> None:
    env = _git_env()
    _run("git", "checkout", "-b", "feat-x", cwd=clone, env=env)


def _make_tracked_dirty(clone: Path) -> None:
    """Modify a tracked file so the working tree is dirty."""
    (clone / "a.txt").write_text("a-modified")


def _seed_origin_changing_a_in_second_commit(origin_dir: Path) -> None:
    """Bare origin whose second commit *changes* ``a.txt`` content.

    Models the #2614 incident's upstream side: a path's new content lands on
    ``origin/main`` via a proper PR (second commit). A clone parked at the
    first commit can then carry a dirty working copy of that same path whose
    blob is byte-identical to the upstream version — a stale duplicate.
    """
    env = _git_env()
    seed = origin_dir.parent / f"_seed_{origin_dir.name}"
    seed.mkdir()
    _run("git", "init", "--initial-branch=main", str(seed), cwd=origin_dir.parent, env=env)
    (seed / "a.txt").write_text("a-original")
    (seed / "c.txt").write_text("c-original")
    _run("git", "add", "a.txt", "c.txt", cwd=seed, env=env)
    _run("git", "commit", "-m", "first", cwd=seed, env=env)
    (seed / "a.txt").write_text("a-upstream-v2")
    (seed / "b.txt").write_text("b")
    _run("git", "add", "a.txt", "b.txt", cwd=seed, env=env)
    _run("git", "commit", "-m", "second changes a.txt", cwd=seed, env=env)
    _run("git", "init", "--bare", "--initial-branch=main", str(origin_dir), cwd=origin_dir.parent, env=env)
    _run("git", "remote", "add", "origin", str(origin_dir), cwd=seed, env=env)
    _run("git", "push", "-u", "origin", "main", cwd=seed, env=env)


def _origin_blob_for(clone: Path, path: str) -> str:
    """The content ``origin/main`` holds for *path* — the byte-identical target."""
    env = _git_env()
    return _run("git", "show", f"origin/main:{path}", cwd=clone, env=env).stdout


def _stage_path(clone: Path, path: str) -> None:
    env = _git_env()
    _run("git", "add", path, cwd=clone, env=env)


def _make_diverged_non_ff(clone: Path) -> str:
    """Put a local commit on the clone's default branch so the pull is non-ff.

    The clone is one behind ``origin/main`` (a merge advanced origin) and
    *also* has a local-only commit — the two histories diverged, so a
    ``pull --ff-only`` cannot fast-forward and exits non-zero. The scanner
    must skip it, never reset/force. Returns the local diverged HEAD SHA.
    """
    env = _git_env()
    (clone / "local-only.txt").write_text("local")
    _run("git", "add", "local-only.txt", cwd=clone, env=env)
    _run("git", "commit", "-m", "local divergence", cwd=clone, env=env)
    return _run("git", "rev-parse", "HEAD", cwd=clone, env=env).stdout.strip()


def _head_sha(clone: Path) -> str:
    env = _git_env()
    return _run("git", "rev-parse", "HEAD", cwd=clone, env=env).stdout.strip()


def _tracked_dirty(clone: Path) -> bool:
    """True iff the clone has uncommitted *tracked* changes (staged or unstaged)."""
    env = _git_env()
    porcelain = _run("git", "status", "--porcelain", cwd=clone, env=env).stdout
    return any(line and not line.startswith("??") for line in porcelain.splitlines())


def _origin_head(origin: Path, parent: Path) -> str:
    return _run("git", "-C", str(origin), "rev-parse", "main", cwd=parent).stdout.strip()


def _rmtree_safe(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


class PullMainCloneScannerBehaviorTests(TestCase):
    """Per-repo decision-ladder cases on real ``tmp_path`` git clones."""

    def setUp(self) -> None:
        self._tmp = Path(self._make_tempdir())
        self.origin = self._tmp / "origin.git"
        _seed_origin_with_two_commits(self.origin)
        # A second origin whose second commit *changes* a.txt — used by the
        # #2614 self-heal cases that need an upstream-evolved tracked path.
        self._tmp_changing = Path(self._make_tempdir())
        self.origin_changing = self._tmp_changing / "origin.git"
        _seed_origin_changing_a_in_second_commit(self.origin_changing)

    def _make_tempdir(self) -> str:
        d = tempfile.mkdtemp(prefix="pull_main_clone_scanner_")
        self.addCleanup(_rmtree_safe, d)
        return d

    def _scanner(self, *, repos: list[tuple[str, Path]], cadence_hours: int = 1) -> PullMainCloneScanner:
        return PullMainCloneScanner(repos=tuple(repos), cadence_hours=cadence_hours)

    def test_clean_default_branch_behind_origin_is_fast_forwarded(self) -> None:
        """The canonical success path: a merge advanced origin, the clone ff-pulls."""
        clone = self._tmp / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        origin_head = _origin_head(self.origin, self._tmp)
        assert old_sha != origin_head

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == origin_head, "scanner did not fast-forward the clone"
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "pull_main_clone.updated"
        assert signal.payload["repo"] == "acme:acme-backend"
        assert signal.payload["old_sha"].startswith(old_sha[:7])
        assert signal.payload["new_sha"].startswith(origin_head[:7])

    def test_already_up_to_date_emits_no_updated_signal(self) -> None:
        """Idempotent: an already-current clone is a no-op ``up_to_date``, not ``updated``."""
        clone = self._tmp / "acme-backend"
        _clone_up_to_date(origin=self.origin, clone=clone)
        before = _head_sha(clone)

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == before
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.up_to_date"

    def test_dirty_tracked_tree_is_skipped_never_clobbered(self) -> None:
        """Tracked modifications block the ff-pull — never reset/stash local work."""
        clone = self._tmp / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _make_tracked_dirty(clone)

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == old_sha, "scanner clobbered a dirty tree"
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "pull_main_clone.skipped"
        assert "dirty" in signal.payload["reason"] or "tracked" in signal.payload["reason"]

    def test_byte_identical_dirty_blob_is_auto_healed_then_pulled(self) -> None:
        """#2614 self-heal: a dirty path whose blob == ``origin/main``'s is discarded, then ff-pulled.

        The stale-duplicate incident — the path's content already merged upstream
        via a proper PR, leaving the local working copy byte-identical to
        ``origin/main``. Discarding it is provably data-loss-free (the content is
        already upstream), so the scanner auto-discards and proceeds with the FF
        pull instead of skipping the tick forever.
        """
        clone = self._tmp_changing / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin_changing, clone=clone)
        # Make a.txt byte-identical to origin/main's version: a stale duplicate.
        (clone / "a.txt").write_text(_origin_blob_for(clone, "a.txt"))
        origin_head = _origin_head(self.origin_changing, self._tmp_changing)
        assert old_sha != origin_head

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == origin_head, "scanner did not ff-pull after auto-healing the duplicate"
        assert not _tracked_dirty(clone), "the stale duplicate was not discarded"
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.updated"

    def test_byte_identical_staged_dirty_blob_is_auto_healed_then_pulled(self) -> None:
        """The exact incident shape — the duplicate was ``git add``-staged, not just unstaged."""
        clone = self._tmp_changing / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin_changing, clone=clone)
        (clone / "a.txt").write_text(_origin_blob_for(clone, "a.txt"))
        _stage_path(clone, "a.txt")
        origin_head = _origin_head(self.origin_changing, self._tmp_changing)
        assert old_sha != origin_head

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == origin_head, "scanner did not ff-pull after auto-healing the staged duplicate"
        assert not _tracked_dirty(clone), "the staged stale duplicate was not discarded"
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.updated"

    def test_genuinely_different_dirty_blob_keeps_skip_and_warning(self) -> None:
        """A dirty path whose blob DIFFERS from ``origin/main`` keeps the safe skip — never reset.

        The data-loss guard: genuine local work whose content is NOT upstream must
        be preserved. The scanner skips with the ``dirty_tracked`` warning exactly
        as before — it never resets, forces, or stashes a differing blob.
        """
        clone = self._tmp_changing / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin_changing, clone=clone)
        (clone / "a.txt").write_text("genuine-local-work-not-upstream")

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == old_sha, "scanner moved HEAD despite genuine local work"
        assert (clone / "a.txt").read_text() == "genuine-local-work-not-upstream", "genuine work was clobbered"
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "pull_main_clone.skipped"
        assert "dirty" in signal.payload["reason"] or "tracked" in signal.payload["reason"]

    def test_mixed_dirty_one_identical_one_genuine_keeps_skip(self) -> None:
        """A mix — one duplicate path + one genuine path — keeps the safe skip; nothing reset.

        The self-heal acts ONLY when *every* dirty path is provably upstream. A
        single genuinely-different path keeps the whole skip-with-warning, so the
        duplicate path is left untouched too — never a partial reset.
        """
        clone = self._tmp_changing / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin_changing, clone=clone)
        # a.txt is a byte-identical duplicate of origin/main; c.txt is genuine work.
        (clone / "a.txt").write_text(_origin_blob_for(clone, "a.txt"))
        (clone / "c.txt").write_text("genuine-local-edit-not-upstream")

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == old_sha, "scanner moved HEAD despite one genuine dirty path"
        assert (clone / "c.txt").read_text() == "genuine-local-edit-not-upstream", "genuine work was clobbered"
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.skipped"

    def test_worktree_matches_origin_but_staged_blob_differs_keeps_skip(self) -> None:
        """Worktree == origin, but a *staged* blob differs from BOTH origin and HEAD → skip.

        The data-loss edge the index check guards: the visible working-tree blob
        is byte-identical to ``origin/main``, yet the index carries a third,
        genuinely-different staged blob. Discarding would drop that staged work,
        so the self-heal must NOT fire — the whole set keeps the safe skip.
        """
        clone = self._tmp_changing / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin_changing, clone=clone)
        # Stage a genuinely-different blob, THEN make the worktree match origin.
        (clone / "a.txt").write_text("staged-genuine-work-differs-from-everything")
        _stage_path(clone, "a.txt")
        (clone / "a.txt").write_text(_origin_blob_for(clone, "a.txt"))

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == old_sha, "scanner moved HEAD despite a differing staged blob"
        index_blob = _run("git", "show", ":a.txt", cwd=clone, env=_git_env()).stdout
        assert index_blob == "staged-genuine-work-differs-from-everything", "staged work was clobbered"
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.skipped"

    def test_feature_branch_checkout_is_skipped(self) -> None:
        """A main clone parked on a feature branch is skipped — work-in-flight is sacred."""
        clone = self._tmp / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _checkout_feature_branch(clone)

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == old_sha
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "pull_main_clone.skipped"
        assert "branch" in signal.payload["reason"].lower()

    def test_non_fast_forward_is_skipped_never_forced(self) -> None:
        """A diverged clone (local commit + origin advanced) is skipped, never force-reset."""
        clone = self._tmp / "acme-backend"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        local_head = _make_diverged_non_ff(clone)
        origin_head = _origin_head(self.origin, self._tmp)
        assert local_head != origin_head

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == local_head, "scanner force-moved a diverged clone"
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.failed"

    def test_cadence_not_elapsed_skips_without_running_git(self) -> None:
        """A recent marker within the cadence window short-circuits the scan."""
        clone = self._tmp / "acme-backend"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        PullMainCloneMarker.objects.create(
            repo_label="acme:acme-backend",
            repo_path=str(clone),
            last_outcome="updated",
            last_pulled_sha="deadbeef",
            last_pull_at=timezone.now() - _dt.timedelta(minutes=5),
        )
        old_sha = _head_sha(clone)

        signals = self._scanner(repos=[("acme:acme-backend", clone)], cadence_hours=1).scan()

        assert _head_sha(clone) == old_sha, "cadence gate did not prevent the git work"
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.cadence_not_elapsed"

    def test_cadence_elapsed_runs_the_pull(self) -> None:
        """An old marker re-enables the per-tick pull."""
        clone = self._tmp / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        PullMainCloneMarker.objects.create(
            repo_label="acme:acme-backend",
            repo_path=str(clone),
            last_outcome="up_to_date",
            last_pulled_sha=old_sha,
            last_pull_at=timezone.now() - _dt.timedelta(hours=2),
        )

        signals = self._scanner(repos=[("acme:acme-backend", clone)], cadence_hours=1).scan()

        assert _head_sha(clone) != old_sha
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.updated"

    def test_marker_persisted_after_successful_pull(self) -> None:
        """The post-pull marker records the new SHA + ``updated`` outcome."""
        clone = self._tmp / "acme-backend"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)

        self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        marker = PullMainCloneMarker.objects.get(repo_label="acme:acme-backend")
        assert marker.last_outcome == "updated"
        assert marker.last_pulled_sha == _head_sha(clone)
        assert (timezone.now() - marker.last_pull_at).total_seconds() < 60

    def test_marker_persisted_after_skip(self) -> None:
        """Even a skip writes a marker — the cadence gate needs *something* to read."""
        clone = self._tmp / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _checkout_feature_branch(clone)

        self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        marker = PullMainCloneMarker.objects.get(repo_label="acme:acme-backend")
        assert marker.last_outcome == "skipped"
        assert marker.last_pulled_sha == old_sha

    def test_per_repo_iteration_isolates_failures(self) -> None:
        """One repo's skip does not stop the scanner from pulling siblings."""
        clone_a = self._tmp / "acme-backend"
        clone_b = self._tmp / "acme-frontend"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone_a)
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone_b)
        _checkout_feature_branch(clone_a)  # A skips; B should still pull.

        signals = self._scanner(repos=[("acme:be", clone_a), ("acme:fe", clone_b)]).scan()

        kinds = sorted(s.kind for s in signals)
        assert kinds == ["pull_main_clone.skipped", "pull_main_clone.updated"]
        assert PullMainCloneMarker.objects.filter(repo_label="acme:be").exists()
        assert PullMainCloneMarker.objects.filter(repo_label="acme:fe").exists()

    def test_missing_repo_path_emits_failed_signal(self) -> None:
        """A repo path that does not exist on disk is a fail, not a crash."""
        missing = self._tmp / "does-not-exist"

        signals = self._scanner(repos=[("acme:gone", missing)]).scan()

        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.failed"

    def test_scanner_name_is_pull_main_clone(self) -> None:
        """The scanner identifies itself for the dispatcher's logs."""
        assert PullMainCloneScanner(repos=()).name == "pull_main_clone"

    def test_unreachable_origin_emits_failed_signal(self) -> None:
        """A clone whose origin remote has vanished is a fetch ``failed``, not a crash."""
        clone = self._tmp / "acme-backend"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        # Point origin at a path that does not exist → ``git fetch origin`` fails.
        env = _git_env()
        _run("git", "remote", "set-url", "origin", str(self._tmp / "gone.git"), cwd=clone, env=env)

        signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert _head_sha(clone) == old_sha
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "pull_main_clone.failed"
        assert signal.payload["reason"].startswith("fetch:")

    def test_no_origin_head_is_skipped(self) -> None:
        """An unresolvable ``origin/HEAD`` is skipped — the default branch is unknown.

        ``git clone`` always seeds ``origin/HEAD`` and ``git fetch`` restores
        it, so the only honest way to exercise this gate against a real clone
        is to make ``_default_branch`` return ``None`` (the contract for a
        repo whose ``symbolic-ref refs/remotes/origin/HEAD`` does not
        resolve). The gate is otherwise driven by real git.
        """
        from unittest.mock import patch  # noqa: PLC0415

        clone = self._tmp / "acme-backend"
        _clone_up_to_date(origin=self.origin, clone=clone)
        with patch("teatree.loop.scanners.pull_main_clone._default_branch", return_value=None):
            signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "pull_main_clone.skipped"
        assert signal.payload["reason"] == "no_origin_head"

    def test_default_branch_returns_none_when_symbolic_ref_unset(self) -> None:
        """``_default_branch`` returns ``None`` for a repo with no ``origin/HEAD``.

        Pins the real-git contract the ``no_origin_head`` gate relies on: a
        bare repo (never cloned, no ``origin/HEAD`` ref) yields ``None``.
        """
        from teatree.loop.scanners.pull_main_clone import _default_branch  # noqa: PLC0415

        bare = self._tmp / "bare-no-head.git"
        _run("git", "init", "--bare", "--initial-branch=main", str(bare), cwd=self._tmp, env=_git_env())
        assert _default_branch(bare) is None

    def test_marker_upsert_failure_does_not_crash_the_scan(self) -> None:
        """A DB error persisting the marker is logged, never propagated — the tick survives."""
        from unittest.mock import patch  # noqa: PLC0415

        clone = self._tmp / "acme-backend"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        with patch(
            "teatree.core.models.pull_main_clone_marker.PullMainCloneMarker.objects.update_or_create",
            side_effect=RuntimeError("db down"),
        ):
            signals = self._scanner(repos=[("acme:acme-backend", clone)]).scan()

        # The pull still happened and the signal still surfaced.
        assert len(signals) == 1
        assert signals[0].kind == "pull_main_clone.updated"
        # No marker row was written because the upsert raised.
        assert not PullMainCloneMarker.objects.filter(repo_label="acme:acme-backend").exists()

    def test_marker_str_is_human_readable(self) -> None:
        """``__str__`` renders label + outcome for log/debug legibility."""
        marker = PullMainCloneMarker.objects.create(
            repo_label="acme:acme-backend",
            last_outcome="updated",
        )
        text = str(marker)
        assert "acme:acme-backend" in text
        assert "updated" in text


class _FakeOverlay:
    """Minimal overlay stand-in exposing the one hook the wiring reads."""

    def __init__(self, workspace_repos: list[str]) -> None:
        self._workspace_repos = workspace_repos

    def get_workspace_repos(self) -> list[str]:
        return list(self._workspace_repos)


class PullMainCloneScannerWiringTests(TestCase):
    """The wiring layer reads the cadence setting and resolves work-repo clones."""

    def test_default_cadence_setting_is_one_hour(self) -> None:
        """``UserSettings.pull_main_clone_cadence_hours`` defaults to 1 hour."""
        from teatree.config import UserSettings  # noqa: PLC0415

        assert UserSettings().pull_main_clone_cadence_hours == 1

    def test_pull_main_clone_disabled_setting_defaults_off(self) -> None:
        """``pull_main_clone_disabled`` defaults to ``False`` — scanner on by default."""
        from teatree.config import UserSettings  # noqa: PLC0415

        assert UserSettings().pull_main_clone_disabled is False

    def test_settings_are_overlay_overridable(self) -> None:
        """Both knobs are registered for per-overlay ``[overlays.<name>]`` overrides."""
        from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS  # noqa: PLC0415

        assert "pull_main_clone_disabled" in OVERLAY_OVERRIDABLE_SETTINGS
        assert "pull_main_clone_cadence_hours" in OVERLAY_OVERRIDABLE_SETTINGS

    def test_wiring_resolves_workspace_repos_to_main_clones(self) -> None:
        """The helper resolves each workspace repo to its on-disk main clone.

        Repos that resolve to a real clone are included with a namespaced
        ``<overlay>:<repo>`` label; repos with no clone on disk are dropped.
        """
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.scanner_factories import _pull_main_clone_scanner_for  # noqa: PLC0415

        workspace = Path(tempfile.mkdtemp(prefix="pull_main_clone_wiring_"))
        self.addCleanup(_rmtree_safe, str(workspace))
        # acme-backend resolves to a real clone; acme-missing has none.
        origin = workspace / "origin.git"
        _seed_origin_with_two_commits(origin)
        clone = workspace / "acme-backend"
        _clone_up_to_date(origin=origin, clone=clone)

        overlay = _FakeOverlay(["acme-backend", "acme-missing"])
        backend = OverlayBackends(name="acme", overlay=overlay)
        with (
            patch(
                "teatree.loop.scanner_factories._effective_settings_for_overlay",
                return_value=UserSettings(pull_main_clone_cadence_hours=4),
            ),
            patch("teatree.loop.scanner_factories.workspace_dir", return_value=workspace),
        ):
            scanner = _pull_main_clone_scanner_for(backend)

        assert scanner is not None
        assert scanner.cadence_hours == 4
        labels = [label for label, _ in scanner.repos]
        assert labels == ["acme:acme-backend"]
        assert scanner.repos[0][1] == clone

    def test_wiring_returns_none_when_disabled(self) -> None:
        """Escape hatch — ``pull_main_clone_disabled=True`` → no scanner."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.scanner_factories import _pull_main_clone_scanner_for  # noqa: PLC0415

        backend = OverlayBackends(name="acme", overlay=_FakeOverlay(["acme-backend"]))
        with patch(
            "teatree.loop.scanner_factories._effective_settings_for_overlay",
            return_value=UserSettings(pull_main_clone_disabled=True),
        ):
            scanner = _pull_main_clone_scanner_for(backend)
        assert scanner is None

    def test_wiring_returns_none_when_overlay_has_no_python_class(self) -> None:
        """An overlay backend with no Python class has no workspace repos to walk."""
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.scanner_factories import _pull_main_clone_scanner_for  # noqa: PLC0415

        backend = OverlayBackends(name="acme", overlay=None)
        assert _pull_main_clone_scanner_for(backend) is None

    def test_wiring_returns_none_when_no_clone_resolves(self) -> None:
        """All workspace repos missing on disk → nothing to pull, no scanner."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.scanner_factories import _pull_main_clone_scanner_for  # noqa: PLC0415

        empty_workspace = Path(tempfile.mkdtemp(prefix="pull_main_clone_empty_"))
        self.addCleanup(_rmtree_safe, str(empty_workspace))
        backend = OverlayBackends(name="acme", overlay=_FakeOverlay(["acme-backend"]))
        with (
            patch(
                "teatree.loop.scanner_factories._effective_settings_for_overlay",
                return_value=UserSettings(),
            ),
            patch("teatree.loop.scanner_factories.workspace_dir", return_value=empty_workspace),
        ):
            scanner = _pull_main_clone_scanner_for(backend)
        assert scanner is None

    def test_jobs_for_overlay_backend_wires_pull_main_clone(self) -> None:
        """The per-overlay fan-out includes the pull-main-clone scanner when built.

        The sibling per-overlay builders are patched to ``None`` so the test
        isolates the pull-main-clone wiring from the rest of the fan-out.
        """
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415
        from teatree.loop.domain_jobs import _jobs_for_overlay_backend  # noqa: PLC0415

        fake_scanner = PullMainCloneScanner(repos=(("acme:acme-backend", Path("/x")),), cadence_hours=1)
        backend = OverlayBackends(name="acme", overlay=_FakeOverlay(["acme-backend"]))
        with (
            patch("teatree.loop.domain_jobs._pull_main_clone_scanner_for", return_value=fake_scanner),
            patch("teatree.loop.domain_jobs._architectural_review_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._pr_sweep_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._codex_review_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._slack_broadcasts_scanner_for", return_value=None),
            patch("teatree.loop.domain_jobs._failed_e2e_scanner_for", return_value=None),
        ):
            jobs = _jobs_for_overlay_backend(backend)

        assert any(j.scanner is fake_scanner and j.overlay == "acme" for j in jobs)
