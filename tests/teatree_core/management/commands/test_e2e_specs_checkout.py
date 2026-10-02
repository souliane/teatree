"""Two concurrent E2E runs must not be able to corrupt each other's specs tree.

The defect this pins: every ``t3 <overlay> e2e external`` run prepared its specs
by ``git reset --hard FETCH_HEAD`` into ONE shared clone keyed only by repo name,
with no isolation and no lock. Two agents running different branches interleaved
their resets inside a single 13-minute run, so one agent's spec executed against
the other's tree and reported a scenario RED while the fix it was testing sat in
the tree it had been reset away from — a confident, plausible, wrong answer.

Every test drives real ``git`` under ``tmp_path``: the bug is a property of what
git does to a shared working tree, so a mocked ``subprocess.run`` cannot see it.
"""

import fcntl
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import teatree.core.management.commands._e2e_runners as e2e_runners_mod
import teatree.core.management.commands._e2e_specs_checkout as e2e_specs_mod
from teatree.config import E2ERepo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([_GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


_GIT = "git"

#: One branch per key, each carrying a DIFFERENT spec body. The distinct marker is
#: what makes a swapped tree observable at all — identical branches would make the
#: corruption invisible, which is how the real incident produced a plausible result.
_TWO_AGENTS = {"agent-a/fix": "AGENT-A-SPEC", "agent-b/other": "AGENT-B-SPEC"}

#: An mtime comfortably past "now", so a checkout used DURING a held run outranks it.
_FAR_FUTURE = 4_000_000_000.0


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A real upstream repo whose branches each carry their own spec body."""
    repo = tmp_path / "upstream"
    (repo / "e2e").mkdir(parents=True)
    subprocess.run([_GIT, "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "e2e" / "spec.ts").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    for branch, marker in (*_TWO_AGENTS.items(), ("shared/ref", "SHARED-SPEC"), ("team/spec", "SLASH")):
        _git(repo, "checkout", "-q", "-b", branch)
        (repo / "e2e" / "spec.ts").write_text(f"{marker}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", marker)
        _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "team-spec", "team/spec")
    return repo


@pytest.fixture
def data_dir(tmp_path: Path) -> Iterator[Path]:
    """Redirect the runner's cache root into ``tmp_path``."""
    root = tmp_path / "data"

    def resolve(namespace: str) -> Path:
        path = root / namespace
        path.mkdir(parents=True, exist_ok=True)
        return path

    with patch.object(e2e_runners_mod, "get_data_dir", side_effect=resolve):
        yield root

    e2e_specs_mod.release_process_locks()


def _repo(upstream: Path, branch: str) -> E2ERepo:
    return E2ERepo(name="specs-repo", url=str(upstream), branch=branch, e2e_dir="e2e")


def _body(specs_root: Path) -> str:
    return (specs_root / "spec.ts").read_text().strip()


@pytest.mark.usefixtures("data_dir")
class TestConcurrentPreparationsAreIsolated:
    """The reproduction: preparing a second ref must not move the first run's tree."""

    def test_a_second_run_on_another_ref_does_not_move_the_first_runs_tree(self, upstream: Path) -> None:
        """Agent A's specs must still be A's after agent B prepares a different branch.

        The observed collision, reduced: A prepares, B prepares, and A's working
        tree — the directory A is about to hand Playwright as ``cwd`` — is read
        back. Against the shared clone, A reads B's spec body.
        """
        a_specs = e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "agent-a/fix"))
        assert _body(a_specs) == "AGENT-A-SPEC"

        e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "agent-b/other"))

        assert _body(a_specs) == "AGENT-A-SPEC", (
            "agent B's preparation moved agent A's specs tree — A's run would execute "
            "B's code and report a result about a tree that is not its own"
        )

    def test_two_refs_resolve_to_different_checkouts(self, upstream: Path) -> None:
        """Isolation is structural: distinct refs never share a working directory."""
        a_specs = e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "agent-a/fix"))
        b_specs = e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "agent-b/other"))

        assert a_specs != b_specs
        assert _body(b_specs) == "AGENT-B-SPEC"

    def test_refs_differing_only_by_separator_do_not_collide(self, upstream: Path) -> None:
        """``team/spec`` and ``team-spec`` are different refs, so different directories."""
        slash = e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "team/spec"))
        dash = e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "team-spec"))

        assert slash != dash

    def test_the_same_ref_reuses_one_warm_checkout(self, upstream: Path) -> None:
        """Cost control: a repeat run of one ref must not pay for a second install.

        Isolation keyed per RUN would make every run re-install ``node_modules``;
        keying on the ref keeps the common case free.
        """
        first = e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "agent-a/fix"))
        (first / "node_modules").mkdir()
        second = e2e_runners_mod.clone_or_update_e2e_repo(_repo(upstream, "agent-a/fix"))

        assert first == second
        assert (second / "node_modules").is_dir()


class TestRetentionNeverReapsALiveCheckout:
    """Per-ref isolation must not trade a correctness bug for a disk-exhaustion one.

    The reaper deletes directories, so what it must NEVER touch matters more than
    what it collects.
    """

    def _checkout(self, root: Path, ref: str, *, age: float) -> Path:
        path = e2e_specs_mod.checkout_path(root, "specs-repo", ref)
        path.mkdir(parents=True)
        (path / "spec.ts").write_text(ref)
        lock = e2e_specs_mod.lock_path(root, "specs-repo", ref)
        lock.write_text("0\n")
        os.utime(lock, (age, age))
        return path

    def test_only_the_most_recently_used_checkouts_survive(self, tmp_path: Path) -> None:
        for index, ref in enumerate(("oldest", "middle", "newest")):
            self._checkout(tmp_path, ref, age=1000.0 + index)

        e2e_specs_mod.prune_stale_checkouts(tmp_path, "specs-repo", keep=2)

        assert not e2e_specs_mod.checkout_path(tmp_path, "specs-repo", "oldest").exists()
        assert e2e_specs_mod.checkout_path(tmp_path, "specs-repo", "middle").exists()
        assert e2e_specs_mod.checkout_path(tmp_path, "specs-repo", "newest").exists()

    def test_a_checkout_a_live_run_holds_is_never_reaped(self, tmp_path: Path) -> None:
        """A long suite whose ref went stale mid-run is still somebody's running suite.

        Acquiring a checkout refreshes its recency, so the guard only bites once
        enough OTHER refs have been used during a long run to push the held one out
        of the retention window — which is exactly the 13-minute-run case.
        """
        for ref in ("held", "used-later", "used-last"):
            self._checkout(tmp_path, ref, age=1000.0)

        # The live run is another PROCESS, stood in for by a descriptor this one does not own.
        rival = os.open(e2e_specs_mod.lock_path(tmp_path, "specs-repo", "held"), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for index, ref in enumerate(("used-later", "used-last")):
                lock = e2e_specs_mod.lock_path(tmp_path, "specs-repo", ref)
                os.utime(lock, (_FAR_FUTURE + index, _FAR_FUTURE + index))
            removed = e2e_specs_mod.prune_stale_checkouts(tmp_path, "specs-repo", keep=1)
        finally:
            os.close(rival)

        assert e2e_specs_mod.checkout_path(tmp_path, "specs-repo", "held").exists()
        # The control: the prune really did reach past `keep`, so `held` was spared
        # by its lock — not by a no-op pass that would pass for the wrong reason.
        assert removed == [e2e_specs_mod.checkout_path(tmp_path, "specs-repo", "used-later")]

    def test_the_reap_holds_the_lock_for_the_whole_delete(self, tmp_path: Path) -> None:
        """A probe that acquires and releases leaves a window before the ``rmtree``.

        In that window a rival claims the checkout and starts executing against it,
        and the reaper deletes the tree underneath — the same check-then-act race,
        on the same trees, that per-ref isolation exists to close.
        """
        self._checkout(tmp_path, "doomed", age=1000.0)
        self._checkout(tmp_path, "kept", age=_FAR_FUTURE)
        rival_claimed_mid_delete: list[bool] = []
        real_rmtree = shutil.rmtree

        def rmtree_probing_the_lock(path: Path) -> None:
            fd = os.open(e2e_specs_mod.lock_path(tmp_path, "specs-repo", "doomed"), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                rival_claimed_mid_delete.append(True)
            except BlockingIOError:
                rival_claimed_mid_delete.append(False)
            finally:
                os.close(fd)
            real_rmtree(path)

        with patch.object(shutil, "rmtree", side_effect=rmtree_probing_the_lock):
            removed = e2e_specs_mod.prune_stale_checkouts(tmp_path, "specs-repo", keep=1)

        # The control: the reap really did reach `doomed`, so the probe ran at all.
        assert removed == [e2e_specs_mod.checkout_path(tmp_path, "specs-repo", "doomed")]
        assert rival_claimed_mid_delete == [False], (
            "a rival claimed the checkout while the reaper was deleting it — the reap must "
            "HOLD that checkout's own lock, not merely probe it and then delete"
        )

    def test_pruning_an_unknown_repo_is_a_no_op(self, tmp_path: Path) -> None:
        assert e2e_specs_mod.prune_stale_checkouts(tmp_path, "never-cloned") == []


@pytest.mark.usefixtures("data_dir")
class TestTheProductionSeamSerialises:
    """`resolve_external_specs_path` is what the CLI calls — the guard must live THERE.

    A lock released when preparation returns would leave the tree unguarded for the
    whole run, which is the entire window the collision happened in.
    """

    def test_the_claim_outlives_the_call_that_made_it(self, upstream: Path) -> None:
        overlay_repo = _repo(upstream, "shared/ref")
        e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=overlay_repo)

        # Nothing is holding a `with` open here — the first call has already returned,
        # exactly as it has when Playwright starts.
        assert e2e_specs_mod.is_locked(
            e2e_runners_mod.get_data_dir(e2e_specs_mod.SPECS_NAMESPACE), "specs-repo", "shared/ref"
        )

    def test_a_rival_run_on_the_same_ref_is_refused_not_served(self, upstream: Path) -> None:
        """A rival is another PROCESS, stood in for by a descriptor this process does not own."""
        root = e2e_runners_mod.get_data_dir(e2e_specs_mod.SPECS_NAMESPACE)
        lock = e2e_specs_mod.lock_path(root, "specs-repo", "shared/ref")
        lock.parent.mkdir(parents=True, exist_ok=True)
        rival = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(e2e_runners_mod.SpecsCheckoutBusyError):
                e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=_repo(upstream, "shared/ref"))
        finally:
            os.close(rival)

    def test_re_preparing_the_same_ref_in_one_run_is_not_a_rival(self, upstream: Path) -> None:
        overlay_repo = _repo(upstream, "shared/ref")
        first = e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=overlay_repo)
        second = e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=overlay_repo)

        assert first == second

    def test_a_rival_run_on_another_ref_is_served_immediately(self, upstream: Path) -> None:
        """The collision that actually happened: two different branches, no contention."""
        a = e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=_repo(upstream, "agent-a/fix"))
        b = e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=_repo(upstream, "agent-b/other"))

        assert _body(a) == "AGENT-A-SPEC"
        assert _body(b) == "AGENT-B-SPEC"
