"""Dormant-artifact eviction — the lossless reclaim, and the guards that keep it lossless (#4244).

Real ``git`` checkouts under ``tmp_path``, because the population comes from the
filesystem scan and a mocked one would prove nothing about whether an untracked
ad-hoc checkout is covered (roughly half the reclaimable artifacts on the box that
produced this issue sit in checkouts no ledger knows about).

Two load-bearing cases must NOT reclaim. An artifact is not written to while it is
being used, so an in-use one reads as idle by mtime — every keep-case here is
therefore driven through the process table, and the pass is refused outright when
that table cannot answer. And an overlay-provisioned artifact is a SYMLINK at the
main clone's, which every primitive this module uses reads straight through.
"""

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from fnmatch import fnmatchcase
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup import artifact_eviction, artifact_removal, process_table
from teatree.core.cleanup.artifact_eviction import (
    _ARTIFACT_NAMES,
    _MAX_EVICTIONS_PER_PASS,
    ArtifactCandidate,
    ArtifactEvictionPlan,
    EvictionOutcome,
    evict_artifacts,
    plan_artifact_eviction,
)
from teatree.core.cleanup.artifact_lock import artifact_source_lock
from teatree.core.cleanup.checkout_registry import _NEVER_A_CHECKOUT, CheckoutRegistry
from teatree.core.cleanup.reclaim_pressure import effective_idle_days
from tests._git_repo import make_git_repo, run_git
from tests._hook_env import HOOK_ENV_NAME
from tests._process_table_venue import blinded_process_table, holding, this_process_in

_REGISTRY = "teatree.core.cleanup.checkout_registry"
_LONG_AGO = 1_600_000_000  # comfortably beyond any idle threshold under test

# Spelled out rather than read from the module: a coverage test driven by the very
# constant it is meant to pin passes at any width, including the two-name one.
_EVICTABLE = (".venv", ".venv-hook*", "node_modules", ".nx", ".angular")

#: One directory name per entry above. The hook environment is platform-scoped, so no
#: checkout ever holds the pattern's own spelling and a planting test cannot use it.
_EVICTABLE_ON_DISK = (".venv", HOOK_ENV_NAME, "node_modules", ".nx", ".angular")


def _age(*paths: Path) -> None:
    for path in paths:
        os.utime(path, (_LONG_AGO, _LONG_AGO))


#: What each name's documented rebuild command looks for. Planted by default, because
#: every case except the rebuildability ones is about a NORMAL checkout — one where the
#: artifact really is restorable — and omitting it would make them pass for the wrong
#: reason.
_REBUILD_INPUT = {".venv": "pyproject.toml", ".venv-hook*": "pyproject.toml", "node_modules": "package-lock.json"}

# The shipped thresholds, so the escalation tests exercise the real decay.
_WARN_GB = 25.0
_CRIT_GB = 10.0
_IDLE_DAYS = 2.0


def _criterion(free_gb: float) -> float | None:
    return effective_idle_days(free_gb=free_gb, warn_gb=_WARN_GB, crit_gb=_CRIT_GB, idle_days=_IDLE_DAYS)


def _touched_an_hour_ago(checkout: Path, artifact: Path) -> None:
    """What a directory some other process rebuilds every few hours looks like."""
    stamp = timezone.now().timestamp() - 3600
    os.utime(artifact, (stamp, stamp))
    os.utime(checkout, (stamp, stamp))


def _manifest_for(name: str) -> str | None:
    """Matched here rather than through the module under test, which owns the same question."""
    return next((manifest for pattern, manifest in _REBUILD_INPUT.items() if fnmatchcase(name, pattern)), None)


def _artifact_in(
    checkout: Path,
    *,
    name: str = ".venv",
    dormant: bool = True,
    size: int = 4096,
    rebuildable: bool = True,
) -> Path:
    artifact = checkout / name
    (artifact / "lib").mkdir(parents=True)
    (artifact / "lib" / "big.so").write_bytes(b"x" * size)
    if rebuildable and (manifest := _manifest_for(name)):
        (checkout / manifest).touch()
    if dormant:
        _age(artifact, checkout)
    return artifact


class _EvictionFixture(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.workspace = tmp_path
        self.host_proc = tmp_path / "host-proc"
        self.host_proc.mkdir()
        self.enterContext(patch.object(process_table, "_HOST_PROC_ROOT", self.host_proc))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(tmp_path,)))
        self.enterContext(patch(f"{_REGISTRY}.Path.cwd", return_value=tmp_path / "nowhere"))
        self.this_process = this_process_in(self.host_proc)

    def _process_working_in(self, directory: Path, *, pid: str = "4242") -> None:
        (self.host_proc / pid).mkdir(parents=True)
        (self.host_proc / pid / "cwd").symlink_to(directory)
        (self.host_proc / pid / "exe").symlink_to(directory / "bin" / "process")

    def _some_process_exists(self) -> None:
        """A readable table with a process placed nowhere near any checkout."""
        self._process_working_in(self.workspace / "unrelated", pid="1")

    def _plan(self, *, idle_days: float | None = 1) -> ArtifactEvictionPlan:
        return plan_artifact_eviction(self.workspace, idle_days=idle_days)


class TestPressureEscalation(_EvictionFixture):
    """Age stops gating as the disk fills; liveness never does (#4644)."""

    def test_a_fresh_artifact_is_evicted_below_the_critical_floor(self) -> None:
        checkout = make_git_repo(self.workspace / "fresh")
        artifact = _artifact_in(checkout, dormant=False)
        self._some_process_exists()

        plan = self._plan(idle_days=_criterion(0.2))
        outcome = evict_artifacts(plan)

        assert not artifact.exists(), "below the floor a rebuildable cache is not worth an age test"
        assert outcome.freed_bytes > 0
        assert (checkout / ".git").exists(), "the checkout holds the work; only the cache goes"

    def test_liveness_keeps_a_held_artifact_at_critical_while_its_free_sibling_goes(self) -> None:
        held = make_git_repo(self.workspace / "held")
        free = make_git_repo(self.workspace / "free")
        _artifact_in(held, dormant=False)
        _artifact_in(free, dormant=False)
        self._process_working_in(held / "src")

        plan = self._plan(idle_days=_criterion(0.2))
        evict_artifacts(plan)

        assert (held / ".venv").exists(), "liveness is the sole guard once age stops gating"
        assert not (free / ".venv").exists(), "and that guard is only meaningful if the pass reaches it"
        assert any("a live process is working inside the checkout" in line for line in plan.kept)

    def test_liveness_refuses_at_every_pressure_level(self) -> None:
        held = make_git_repo(self.workspace / "held")
        artifact = _artifact_in(held, dormant=False)
        self._process_working_in(held / "src")

        for free_gb in (0.2, 17.5, 200.0):
            with self.subTest(free_gb=free_gb):
                evict_artifacts(self._plan(idle_days=_criterion(free_gb)))
                assert artifact.exists()

    def test_an_unrebuildable_artifact_survives_below_the_floor(self) -> None:
        """Pressure relaxes AGE. It must not relax the proof that the delete is recoverable."""
        checkout = make_git_repo(self.workspace / "scratch")
        artifact = _artifact_in(checkout, name="node_modules", dormant=False, rebuildable=False)
        self._some_process_exists()

        plan = self._plan(idle_days=_criterion(0.2))
        evict_artifacts(plan)

        assert artifact.exists()

    def test_an_hourly_rewritten_checkout_does_not_become_permanently_ineligible(self) -> None:
        """Churn faster than ``artifact_idle_days`` used to confer immunity on every pass, forever."""
        churned = make_git_repo(self.workspace / "churned")
        artifact = _artifact_in(churned, dormant=False)
        _touched_an_hour_ago(churned, artifact)
        self._some_process_exists()

        plan = self._plan(idle_days=_criterion(0.2))
        evict_artifacts(plan)

        assert not artifact.exists()
        assert not any("too recently" in line for line in plan.kept)


class TestEviction(_EvictionFixture):
    def test_a_dormant_venv_is_evicted_and_its_checkout_survives(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        venv = _artifact_in(checkout)
        self._some_process_exists()

        plan = self._plan()
        outcome = evict_artifacts(plan)

        assert not venv.exists()
        assert (checkout / ".git").exists(), "the checkout holds the work; only the cache goes"
        assert outcome.freed_bytes > 0
        assert plan.considered == 1

    def test_an_untracked_checkout_is_covered(self) -> None:
        """No ``Worktree`` row exists — a ledger-keyed reaper walks straight past these."""
        adhoc = make_git_repo(self.workspace / "wt-adhoc")
        venv = _artifact_in(adhoc)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not venv.exists()

    def test_every_rebuildable_artifact_name_is_covered(self) -> None:
        """The widening (#4244): venvs were never the whole 165.7 GB — node_modules dwarfs them."""
        checkout = make_git_repo(self.workspace / "clone")
        artifacts = [_artifact_in(checkout, name=name) for name in _EVICTABLE_ON_DISK]
        _age(checkout)
        self._some_process_exists()

        plan = self._plan()
        evict_artifacts(plan)

        assert plan.considered == len(_EVICTABLE_ON_DISK)
        assert [a for a in artifacts if a.exists()] == []

    def test_every_platforms_hook_environment_is_covered(self) -> None:
        """A bind-mounted clone holds one hook environment per platform, plus rebuild husks.

        The name is platform-scoped, so a fixed-name reaper walks past all but its own and
        the ~650 MB each carries.
        """
        checkout = make_git_repo(self.workspace / "clone")
        foreign = _artifact_in(checkout, name=".venv-hook-linux-aarch64")
        husk = _artifact_in(checkout, name=".venv-hook.broken-darwin-1787810455")
        _age(checkout)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not foreign.exists()
        assert not husk.exists()


class TestSymlinkSafety(_EvictionFixture):
    """A provisioned artifact is a LINK at the main clone's, and every primitive follows it.

    Measured: ``is_dir()`` returns True on the link (so it is selected), ``os.walk``
    descends a symlinked top (so it is sized as the clone), and only ``rmtree``'s own
    ``OSError`` — swallowed until now — kept the shared tree alive.
    """

    def _clone_with_worktree_links(self) -> tuple[Path, dict[str, dict[str, Path]]]:
        """A main clone with a live agent in it, and a worktree whose artifacts link at its.

        The clone's own artifacts are DORMANT — the liveness guard is what keeps them,
        exactly as on the real box. Ageing them is what makes the links dormant too
        (``stat`` follows the link), so the symlink guard is the only thing left
        deciding, rather than the idle window standing in for it.
        """
        clone = make_git_repo(self.workspace / "clone")
        shared = {name: _artifact_in(clone, name=name, size=100_000) for name in (".venv", "node_modules")}
        self._process_working_in(clone / "src")
        worktree = make_git_repo(self.workspace / "wt-1234")
        links = {name: worktree / name for name in shared}
        for name, link in links.items():
            link.symlink_to(shared[name])
        _age(worktree)
        return clone, {"shared": shared, "links": links}

    def test_the_symlink_target_survives_and_the_link_is_never_touched(self) -> None:
        clone, paths = self._clone_with_worktree_links()
        shared, links = paths["shared"], paths["links"]

        plan = self._plan()
        evict_artifacts(plan)

        for name, target in shared.items():
            assert (target / "lib" / "big.so").read_bytes() == b"x" * 100_000, f"{name} in {clone} lost its bytes"
        for name, link in links.items():
            assert link.is_symlink(), f"the {name} link is a provisioned artifact the overlay health-checks"
        planned = {candidate.artifact for candidate in plan.candidates}
        assert planned.isdisjoint(links.values()), "a symlinked artifact is not a candidate at all"
        for link in links.values():
            assert any(str(link) in kept and "symlink" in kept for kept in plan.kept), plan.kept

    def test_a_symlinked_artifact_is_never_sized_through_the_link(self) -> None:
        """``os.walk`` descends a symlinked TOP, so sizing one reports the clone's bytes.

        Under size-ordered selection that promotes exactly the candidates that free
        nothing to the front of the queue.
        """
        self._clone_with_worktree_links()

        plan = self._plan()

        assert plan.estimated_bytes == 0, "the only artifacts in the pool are links; nothing is reclaimable"

    def test_an_artifact_that_became_a_symlink_after_planning_is_not_deleted(self) -> None:
        """Provisioning replaces a real dir with a link mid-window — that is what the overlay step does."""
        clone = make_git_repo(self.workspace / "clone")
        shared = _artifact_in(clone, name="node_modules", dormant=False, size=100_000)
        worktree = make_git_repo(self.workspace / "wt-1234")
        artifact = _artifact_in(worktree, name="node_modules")
        self._some_process_exists()

        plan = self._plan()
        assert [candidate.artifact for candidate in plan.candidates] == [artifact], "the control: it was planned"
        artifact.rename(worktree / "node_modules.gone")
        artifact.symlink_to(shared)
        outcome = evict_artifacts(plan)

        assert (shared / "lib" / "big.so").exists(), "the clone's tree must survive the mid-window swap"
        assert artifact.is_symlink()
        assert outcome.freed_bytes == 0
        assert any("became a symlink" in line for line in outcome.skipped), outcome.skipped

    def test_a_refused_delete_is_reported_never_swallowed(self) -> None:
        """A refused ``rmtree`` read exactly like a successful one — no line, no reason."""
        checkout = make_git_repo(self.workspace / "clone")
        venv = _artifact_in(checkout)
        self._some_process_exists()

        plan = self._plan()
        assert plan.candidates, "the control: it was planned for eviction"
        refusal = OSError("Cannot call rmtree on a symbolic link")
        with patch.object(artifact_removal.shutil, "rmtree", side_effect=refusal):
            outcome = evict_artifacts(plan)

        assert venv.exists()
        assert outcome.freed_bytes == 0, "a delete that did not happen freed nothing"
        assert any("symbolic link" in line for line in outcome.skipped), outcome.skipped

    def test_sizing_never_credits_a_link_out_of_the_tree(self) -> None:
        """A ``node_modules`` is full of internal links; their targets are not its bytes."""
        checkout = make_git_repo(self.workspace / "clone")
        outside = self.workspace / "outside.bin"
        outside.write_bytes(b"y" * 500_000)
        artifact = _artifact_in(checkout, name="node_modules", size=4096)
        (artifact / ".bin").mkdir()
        (artifact / ".bin" / "linked").symlink_to(outside)
        _age(artifact, checkout)
        self._some_process_exists()

        plan = self._plan()

        assert plan.estimated_bytes == 4096, "only the tree's own file counts"


class TestSharedSymlinkTargetSafety(_EvictionFixture):
    """The link is guarded; until #4244's follow-up the shared TARGET was not.

    An overlay points EVERY worktree's ``node_modules``/``.venv`` at ONE real directory
    in the main clone. That target is a real directory at a checkout root, so
    the candidate scan selects it and no guard knows other checkouts depend on it:
    ``_in_use_reason`` asks about the sweeping process and the clone's own process
    placement (a node process runs with its cwd in the WORKTREE and its exe under
    ``~/.nvm``, so neither lands in the clone), and ``_dormancy_reason`` reads only the
    clone's own mtimes. Being the largest object on the box, size-ordered selection
    promotes it FIRST.

    ``TestSymlinkSafety._clone_with_worktree_links`` cannot exercise this: it keeps the
    clone's artifacts alive by planting a process inside the clone, so liveness decides
    before the target guard is ever reached.
    """

    def _dormant_clone_target_and_link(self) -> tuple[Path, Path]:
        """A shared target with NO live process anywhere near it and mtimes past the window."""
        clone = make_git_repo(self.workspace / "clone")
        shared = _artifact_in(clone, name="node_modules", size=200_000)
        worktree = make_git_repo(self.workspace / "wt-1234")
        link = worktree / "node_modules"
        link.symlink_to(shared)
        _age(clone, worktree, shared)
        self._some_process_exists()
        return shared, link

    def test_a_dormant_shared_target_is_not_a_candidate(self) -> None:
        shared, _link = self._dormant_clone_target_and_link()

        plan = self._plan()
        evict_artifacts(plan)

        assert (shared / "lib" / "big.so").read_bytes() == b"x" * 200_000, (
            "the shared target was deleted — every worktree's link now dangles at once"
        )
        assert shared not in {candidate.artifact for candidate in plan.candidates}
        assert any(str(shared) in kept for kept in plan.kept), plan.kept

    def test_the_keep_reason_names_the_dependency_not_idleness(self) -> None:
        """A reason that says "dormant" would invite raising the idle window to reclaim it."""
        shared, _link = self._dormant_clone_target_and_link()

        kept = [line for line in self._plan().kept if str(shared) in line]

        assert kept, "the target must be REPORTED, not silently absent"
        assert "symlink" in kept[0], kept
        assert "checkout" in kept[0], kept

    def test_the_guard_survives_a_link_planted_after_planning(self) -> None:
        """The real provisioning sequence: the worktree is created first, its links land later.

        ``workspace ticket`` creates the checkout and ``worktree provision`` symlinks its
        artifacts, so a plan computed between the two sees a checkout carrying no link and
        would delete the clone tree the next step is about to point at. Re-resolving the
        set at deletion is what catches it — the same reason liveness is re-established
        there rather than inherited from a snapshot taken before minutes of walks.
        """
        clone = make_git_repo(self.workspace / "clone")
        shared = _artifact_in(clone, name="node_modules", size=100_000)
        worktree = make_git_repo(self.workspace / "wt-5678")
        self._some_process_exists()

        plan = self._plan()
        assert [candidate.artifact for candidate in plan.candidates] == [shared], "the control: it was planned"
        (worktree / "node_modules").symlink_to(shared)
        outcome = evict_artifacts(plan)

        assert (shared / "lib" / "big.so").exists(), "a link planted mid-window must still be honoured"
        assert outcome.freed_bytes == 0
        assert any(str(shared) in line for line in outcome.skipped), outcome.skipped

    def test_an_unresolvable_link_makes_the_population_incomplete_rather_than_targetless(self) -> None:
        """Failing OPEN here leaves the real target unprotected — the wrong way for this guard.

        The refusal is scoped to the one link: ``resolve`` is the checkout scan's primitive
        too, so blanket-patching it empties the population and the case never arises.
        """
        shared, link = self._dormant_clone_target_and_link()
        real_resolve = Path.resolve
        eloop = OSError("ELOOP")

        def _refuse_that_one_link(path: Path, *, strict: bool = False) -> Path:
            if path == link:
                raise eloop
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", _refuse_that_one_link):
            plan = self._plan()
            evict_artifacts(plan)

        assert any(str(link) in gap for gap in plan.gaps), plan.gaps
        assert shared.exists(), "a link nobody could resolve is not a link pointing nowhere"

    def test_a_plan_carrying_candidates_but_no_population_is_refused(self) -> None:
        """An empty population makes the shared-target guard vacuous — indistinguishable from safe."""
        candidate = ArtifactCandidate(self.workspace / "clone" / "node_modules", self.workspace / "clone", 1)

        outcome = evict_artifacts(ArtifactEvictionPlan(candidates=(candidate,)))

        assert outcome.refusal
        assert outcome.freed_bytes == 0

    def test_a_target_nobody_links_at_is_still_evicted(self) -> None:
        """Anti-vacuity: the guard must key on the link set, not on being in a clone."""
        clone = make_git_repo(self.workspace / "clone")
        unshared = _artifact_in(clone, name="node_modules", size=200_000)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not unshared.exists(), "with no link pointing at it, the artifact is ordinary reclaim"

    def test_a_separate_git_dir_checkout_protects_its_shared_target(self) -> None:
        clone = make_git_repo(self.workspace / "clone")
        shared = _artifact_in(clone, name="node_modules", size=200_000)
        checkout = self.workspace / "standalone"
        checkout.mkdir()
        administrative = self.workspace / "standalone.git"
        run_git(checkout, "init", "-q", "-b", "main", f"--separate-git-dir={administrative}")
        run_git(checkout, "commit", "-q", "--allow-empty", "-m", "initial")
        (checkout / "node_modules").symlink_to(shared)
        _age(clone, checkout, shared)
        self._some_process_exists()

        plan = self._plan()
        outcome = evict_artifacts(plan)

        assert shared.exists()
        assert checkout in plan.checkouts
        assert outcome.freed_bytes == 0


class TestNameSet(_EvictionFixture):
    def test_the_delete_set_and_the_walk_skip_set_are_deliberately_independent(self) -> None:
        """Deriving one from the other would delete repositories.

        ``_NEVER_A_CHECKOUT`` answers "can this directory CONTAIN a checkout?", which
        ``.git`` correctly cannot. The eviction set answers "is this a rebuildable
        build product?", which ``.git`` is the exact inverse of. The divergence runs
        both ways, so neither is a subset of the other.
        """
        assert _ARTIFACT_NAMES == _EVICTABLE
        assert ".git" in _NEVER_A_CHECKOUT
        assert ".git" not in _ARTIFACT_NAMES, "this set authorises deletion — .git holds every commit"
        assert HOOK_ENV_NAME not in _NEVER_A_CHECKOUT

    def test_pycache_is_not_evicted(self) -> None:
        """Excluded by decision: numerous, kilobytes each, and nested rather than root-level."""
        checkout = make_git_repo(self.workspace / "clone")
        pycache = _artifact_in(checkout, name="__pycache__")
        self._some_process_exists()

        plan = self._plan()
        evict_artifacts(plan)

        assert pycache.exists()
        assert plan.candidates == ()

    def test_the_per_pass_cap_grew_with_the_name_set(self) -> None:
        assert _MAX_EVICTIONS_PER_PASS == 50


class TestRebuildable(_EvictionFixture):
    """The "rebuildable" claim was asserted by the name set and never checked (#4244 review).

    ``_ARTIFACT_NAMES`` applies to every ``.git``-carrying directory under the scan
    roots, which includes scratch repos that never had a lockfile — and ``npm ci``
    refuses outright without one. There the documented recovery cannot restore what was
    removed, which is a different kind of loss from the one this pass advertises.
    """

    def test_a_lockfile_less_node_modules_is_kept(self) -> None:
        checkout = make_git_repo(self.workspace / "scratch")
        artifact = _artifact_in(checkout, name="node_modules", rebuildable=False)
        self._some_process_exists()

        plan = self._plan()
        evict_artifacts(plan)

        assert artifact.exists(), "npm ci refuses without a lockfile — this delete is not recoverable"
        assert any("rebuilds it" in kept for kept in plan.kept), plan.kept

    def test_a_node_modules_with_a_lockfile_is_evicted(self) -> None:
        """Anti-vacuity: the guard must key on the manifest, not withhold node_modules wholesale."""
        checkout = make_git_repo(self.workspace / "app")
        artifact = _artifact_in(checkout, name="node_modules")
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not artifact.exists()

    def test_a_manifest_less_venv_is_kept(self) -> None:
        checkout = make_git_repo(self.workspace / "scratch")
        artifact = _artifact_in(checkout, rebuildable=False)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert artifact.exists(), "uv sync needs a manifest to rebuild from"

    def test_a_hook_env_is_kept_on_a_pre_commit_config_alone(self) -> None:
        """A hook environment is a uv PROJECT environment, so only a uv manifest rebuilds it.

        Its name carries the platform, so the rebuild inputs are keyed by the PATTERN it
        matched — keyed by the raw name it would find no entry and read as a free cache.
        """
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, name=HOOK_ENV_NAME, rebuildable=False)
        (checkout / ".pre-commit-config.yaml").touch()
        _age(checkout, artifact)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert artifact.exists(), "a hook config rebuilds hooks, never the uv environment they run in"

    def test_a_globbed_rebuild_input_counts(self) -> None:
        """``requirements-dev.txt`` rebuilds a venv exactly as ``requirements.txt`` does."""
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, rebuildable=False)
        (checkout / "requirements-dev.txt").touch()
        _age(checkout, artifact)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not artifact.exists()

    def test_a_bun_text_lockfile_counts(self) -> None:
        """Bun >= 1.2 writes ``bun.lock``; only the older binary ``bun.lockb`` was listed."""
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, name="node_modules", rebuildable=False)
        (checkout / "bun.lock").touch()
        _age(checkout, artifact)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not artifact.exists()

    def test_a_build_cache_needs_no_manifest(self) -> None:
        """``.nx``/``.angular`` are regenerated by the next build unconditionally.

        Withholding them for a missing manifest would cost reclaim for no safety — there
        is no rebuild command to refuse.
        """
        checkout = make_git_repo(self.workspace / "scratch")
        artifact = _artifact_in(checkout, name=".nx", rebuildable=False)
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not artifact.exists()


class TestSizingBound(_EvictionFixture):
    """Sizing is an ``os.walk`` per candidate, inline in the tick beside two other jobs.

    Ordering the WHOLE eligible set by size paid for a walk of every candidate the pass
    was about to defer — on the box that produced this, up to ~1200 walks per pass with
    the remainder re-walked until the backlog drained.
    """

    def _three_eligible_checkouts(self) -> list[Path]:
        checkouts = [make_git_repo(self.workspace / f"wt-{n}") for n in range(3)]
        for checkout in checkouts:
            _artifact_in(checkout, name="node_modules")
        self._some_process_exists()
        return checkouts

    def test_only_a_bounded_prefix_is_sized(self) -> None:
        self._three_eligible_checkouts()
        real = artifact_eviction._dir_size_bytes

        with (
            patch.object(artifact_eviction, "_MAX_SIZED_PER_PASS", 2),
            patch.object(artifact_eviction, "_MAX_EVICTIONS_PER_PASS", 1),
            patch.object(artifact_eviction, "_dir_size_bytes", side_effect=real) as sizing,
        ):
            plan = self._plan()

        assert sizing.call_count == 2, "the pass sized a candidate it was always going to defer"
        assert len(plan.candidates) == 1

    def test_what_the_bound_deferred_is_named_not_silently_dropped(self) -> None:
        self._three_eligible_checkouts()

        with patch.object(artifact_eviction, "_MAX_SIZED_PER_PASS", 2):
            plan = self._plan()

        assert any("sizing bound" in line for line in plan.deferred), plan.deferred
        assert plan.considered == 3, "every artifact is still CONSIDERED; only the measuring is bounded"

    def test_a_bound_deferral_is_never_reported_as_a_safety_keep(self) -> None:
        """One count for both reads as a guard firing, and invites raising the idle window."""
        self._three_eligible_checkouts()

        with patch.object(artifact_eviction, "_MAX_SIZED_PER_PASS", 2):
            plan = self._plan()

        assert plan.kept == (), "nothing was WITHHELD here — the remainder is only unmeasured"

    def test_the_bounded_prefix_is_deterministic_so_the_backlog_drains(self) -> None:
        """A prefix that moved each pass would churn: sized, deferred, re-sized, never evicted."""
        self._three_eligible_checkouts()

        with patch.object(artifact_eviction, "_MAX_SIZED_PER_PASS", 2):
            first = {candidate.artifact for candidate in self._plan().candidates}
            second = {candidate.artifact for candidate in self._plan().candidates}

        assert first == second


class TestGuards(_EvictionFixture):
    def test_a_checkout_with_a_live_process_keeps_every_artifact(self) -> None:
        """THE guard. An artifact reads as idle by mtime — only the process says otherwise."""
        checkout = make_git_repo(self.workspace / "live")
        artifacts = [_artifact_in(checkout, name=name) for name in _EVICTABLE_ON_DISK]
        _age(checkout)
        self._process_working_in(checkout / "src")

        plan = self._plan()
        evict_artifacts(plan)

        assert [a for a in artifacts if not a.exists()] == []
        assert sum("live process" in kept for kept in plan.kept) == len(_EVICTABLE_ON_DISK)

    def test_an_unreadable_process_table_refuses_the_whole_pass(self) -> None:
        """Fail CLOSED: absence of a live process is the whole authority to delete."""
        checkout = make_git_repo(self.workspace / "clone")
        artifacts = [_artifact_in(checkout, name=name) for name in _EVICTABLE_ON_DISK]

        marker = self.workspace / ".dockerenv"
        marker.touch()
        with patch.object(process_table, "_CONTAINER_MARKERS", (marker,)):
            plan = self._plan()
            evict_artifacts(plan)

        assert plan.refusal, "a pass that cannot see the processes must say so, not return no candidates"
        assert plan.candidates == ()
        assert [a for a in artifacts if not a.exists()] == []

    def test_a_recently_touched_artifact_is_kept(self) -> None:
        checkout = make_git_repo(self.workspace / "fresh")
        artifact = _artifact_in(checkout, name="node_modules", dormant=False)
        self._some_process_exists()

        plan = self._plan()
        evict_artifacts(plan)

        assert artifact.exists()
        assert any("too recently" in kept for kept in plan.kept)

    def test_the_idle_window_narrows_every_name_not_just_venvs(self) -> None:
        aged = make_git_repo(self.workspace / "aged")
        fresh = make_git_repo(self.workspace / "fresh")
        dormant = _artifact_in(aged, name="node_modules")
        recent = _artifact_in(fresh, name="node_modules", dormant=False)
        self._some_process_exists()

        plan = self._plan(idle_days=1)

        assert [candidate.artifact for candidate in plan.candidates] == [dormant]
        assert any(str(recent) in kept and "too recently" in kept for kept in plan.kept)

    def test_an_artifact_whose_checkout_gained_a_process_after_planning_is_not_deleted(self) -> None:
        """Plan and delete are separated by the walks and prunes above — 34-68s on the box that produced this."""
        checkout = make_git_repo(self.workspace / "gained")
        venv = _artifact_in(checkout)
        self._some_process_exists()

        plan = self._plan()
        assert [candidate.artifact for candidate in plan.candidates] == [venv], "the control: it was planned"
        self._process_working_in(checkout / "src", pid="4243")
        outcome = evict_artifacts(plan)

        assert venv.exists(), "an agent that arrived after planning must not have the floor pulled out"
        assert outcome.freed_bytes == 0
        assert any("a live process is working inside the checkout" in line for line in outcome.skipped)

    def test_a_process_table_that_stops_answering_after_planning_refuses_the_eviction(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        venv = _artifact_in(checkout)
        self._some_process_exists()

        plan = self._plan()
        assert plan.candidates, "the control: it was planned for eviction"
        with blinded_process_table(self.workspace / "gone"):
            outcome = evict_artifacts(plan)

        assert venv.exists(), "a table that went blind between planning and deleting authorises nothing"
        assert outcome.refusal

    def test_an_open_file_keeps_the_artifact_when_the_process_cwd_is_elsewhere(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, name="node_modules")
        self._process_working_in(self.workspace / "elsewhere")
        fd = self.host_proc / "4242" / "fd"
        fd.mkdir()
        (fd / "7").symlink_to(artifact / "lib" / "big.so")

        plan = self._plan()
        outcome = evict_artifacts(plan)

        assert artifact.exists()
        assert outcome.freed_bytes == 0

    def test_a_pid_that_goes_mute_mid_batch_does_not_abort_the_batch(self) -> None:
        """A shared box always has mute pids; aborting on one leaves the reclaim inert."""
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout)
        self._some_process_exists()
        plan = self._plan()
        hidden = self.host_proc / "2" / "fd" / "0"
        hidden.parent.mkdir(parents=True)
        hidden.symlink_to(self.workspace / "unknown")
        readlink = Path.readlink
        unreadable = PermissionError()

        def _readlink(path: Path) -> Path:
            if path == hidden:
                raise unreadable
            return readlink(path)

        with patch.object(Path, "readlink", _readlink):
            outcome = evict_artifacts(plan)

        assert not artifact.exists()
        assert not outcome.refusal


class TestDeleteTimeIdentity(_EvictionFixture):
    def test_a_replaced_artifact_directory_is_not_deleted(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, name="node_modules")
        self._some_process_exists()
        plan = self._plan()
        artifact.rename(checkout / "node_modules.before-plan")
        replacement = _artifact_in(checkout, name="node_modules")

        outcome = evict_artifacts(plan)

        assert replacement.exists()
        assert outcome.freed_bytes == 0
        assert outcome.skipped

    def test_a_replaced_checkout_is_not_deleted(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        _artifact_in(checkout, name="node_modules")
        self._some_process_exists()
        plan = self._plan()
        checkout.rename(self.workspace / "clone.before-plan")
        replacement_checkout = make_git_repo(checkout)
        replacement = _artifact_in(replacement_checkout, name="node_modules")

        outcome = evict_artifacts(plan)

        assert replacement.exists()
        assert outcome.freed_bytes == 0
        assert outcome.skipped

    def test_an_artifact_that_lost_its_rebuild_input_is_not_deleted(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, name="node_modules")
        self._some_process_exists()
        plan = self._plan()
        (checkout / "package-lock.json").unlink()

        outcome = evict_artifacts(plan)

        assert artifact.exists()
        assert outcome.freed_bytes == 0
        assert any("rebuild" in line for line in outcome.skipped)

    def test_a_swap_between_authorisation_and_deletion_does_not_remove_the_replacement(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, name="node_modules")
        self._some_process_exists()
        plan = self._plan()
        displaced = checkout / "node_modules.displaced"
        sentinel = artifact / "replacement.txt"
        real_open = artifact_removal.os.open
        real_rmtree = artifact_removal.shutil.rmtree
        swapped = False

        def _swap() -> None:
            nonlocal swapped
            if swapped:
                return
            artifact.rename(displaced)
            artifact.mkdir()
            sentinel.write_text("new", encoding="utf-8")
            swapped = True

        def _open(path: str | Path, flags: int, *args: object, **kwargs: object) -> int:
            descriptor = real_open(path, flags, *args, **kwargs)
            if Path(path) == Path(artifact.name) and kwargs.get("dir_fd") is not None:
                _swap()
            return descriptor

        def _rmtree(path: str | Path, *, dir_fd: int | None = None) -> None:
            if Path(path) == artifact:
                _swap()
            real_rmtree(path, dir_fd=dir_fd)

        with (
            patch.object(artifact_removal.os, "open", side_effect=_open),
            patch.object(artifact_removal.shutil, "rmtree", side_effect=_rmtree),
        ):
            outcome = evict_artifacts(plan)

        assert swapped
        assert sentinel.read_text(encoding="utf-8") == "new"
        assert outcome.freed_bytes == 0
        assert outcome.skipped


class TestSubmoduleSafety(_EvictionFixture):
    def test_a_submodule_refuses_eviction_until_parent_liveness_is_modeled(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        source = make_git_repo(self.workspace / "submodule-source")
        run_git(
            checkout,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(source),
            "vendor/dependency",
        )
        submodule = checkout / "vendor" / "dependency"
        artifact = _artifact_in(submodule, name="node_modules")
        self._process_working_in(checkout / "src")

        plan = self._plan()
        outcome = evict_artifacts(plan)

        assert artifact.exists()
        assert all(candidate.checkout != submodule for candidate in plan.candidates)
        assert plan.refusal
        assert any(str(submodule) in gap and "submodule" in gap for gap in plan.gaps)
        assert outcome.freed_bytes == 0


class TestEnumerationGap(_EvictionFixture):
    """After the shared-target guard, a gap costs SAFETY — the link set comes from that scan.

    The module used to claim a missed checkout is "simply never a candidate", which was
    true while every guard read the candidate's own checkout. The structural guard reads
    the WHOLE population: an unreadable region hides links, and a shared target whose
    linking worktrees all sit in that region is deleted, dangling every link at once.
    Gaps are not hypothetical here — ``checkout_scan_roots`` includes ``Path.home()``
    unconditionally, and macOS TCC refuses ``~/Library`` subtrees on every pass
    (measured: 25).
    """

    def _gap(self, *checkouts: Path) -> AbstractContextManager[object]:
        registry = CheckoutRegistry(frozenset(str(path) for path in checkouts), ("could not scan somewhere",))
        return patch(f"{_REGISTRY}.scan_checkout_paths", return_value=registry)

    def _linked_worktree(self, clone: Path, path: Path) -> Path:
        run_git(clone, "worktree", "add", "-q", "-b", path.name, str(path))
        return path

    def test_a_gap_refuses_the_pass(self) -> None:
        clone = make_git_repo(self.workspace / "clone")
        worktree = self._linked_worktree(clone, self.workspace / "wt-1234")
        _artifact_in(worktree, name="node_modules")
        self._some_process_exists()

        with self._gap(clone, worktree):
            plan = self._plan()

        assert plan.gaps, "what went unread is reported"
        assert plan.refusal
        assert plan.candidates == ()

    def test_a_git_file_cannot_make_a_partial_enumeration_deletable(self) -> None:
        clone = make_git_repo(self.workspace / "clone")
        worktree = self._linked_worktree(clone, self.workspace / "wt-1234")
        artifact = _artifact_in(worktree, name="node_modules")
        self._some_process_exists()

        with self._gap(clone, worktree):
            plan = self._plan()
            outcome = evict_artifacts(plan)

        assert artifact.exists()
        assert plan.refusal
        assert outcome.freed_bytes == 0

    def test_a_gap_keeps_a_clones_artifact_because_a_hidden_link_cannot_be_ruled_out(self) -> None:
        clone = make_git_repo(self.workspace / "clone")
        shared = _artifact_in(clone, name="node_modules")
        self._some_process_exists()

        with self._gap(clone):
            plan = self._plan()
            outcome = evict_artifacts(plan)

        assert shared.exists(), "an unread region can hold the only link at this tree"
        assert outcome.freed_bytes == 0
        assert plan.refusal

    def test_the_same_clone_artifact_is_evicted_once_the_enumeration_is_complete(self) -> None:
        """The RED control: without the gap this is ordinary reclaim, so the keep is the GAP's doing."""
        clone = make_git_repo(self.workspace / "clone")
        shared = _artifact_in(clone, name="node_modules")
        self._some_process_exists()

        evict_artifacts(self._plan())

        assert not shared.exists()


class TestGuardIsPerDeletionNotPerBatch(_EvictionFixture):
    """The window the guard must cover is the DELETE LOOP, not just plan-to-first-delete.

    ``TestGuards``' two after-planning cases each plan ONE candidate, so a snapshot taken
    once at the top of :func:`evict_artifacts` satisfies them: the change they observe
    lands before the only ``rmtree``. A batch is up to 50 ``rmtree`` calls over trees
    reaching tens of GB, so the loop itself is LONGER than the plan-to-delete window
    #4244 was about — and an agent that starts work, or a ``worktree provision`` that
    plants a link, between candidate #1 and candidate #2 is judged against a table read
    minutes earlier.
    """

    def _two_candidates(self, second: str) -> tuple[Path, Path, Path]:
        """A large artifact evicted first, and a smaller one in *second* reached after it."""
        first = make_git_repo(self.workspace / "aaa-first")
        big = _artifact_in(first, size=200_000)
        later = make_git_repo(self.workspace / second)
        small = _artifact_in(later, name="node_modules", size=1_000)
        self._some_process_exists()
        return big, later, small

    def _evict_planting_after_the_first_delete(
        self,
        plan: ArtifactEvictionPlan,
        plant: Callable[[], object],
    ) -> EvictionOutcome:
        """Run the batch, firing *plant* after candidate #1 is removed."""
        real_remove = artifact_eviction._remove_anchored_candidate
        removed: list[Path] = []

        def _remove(candidate: ArtifactCandidate) -> str:
            reason = real_remove(candidate)
            if not reason:
                removed.append(candidate.artifact)
            if len(removed) == 1:
                plant()
            return reason

        with patch.object(artifact_eviction, "_remove_anchored_candidate", side_effect=_remove):
            return evict_artifacts(plan)

    def test_a_process_that_arrives_mid_batch_still_stops_the_next_deletion(self) -> None:
        big, later, small = self._two_candidates("live-later")

        plan = self._plan()
        assert [candidate.artifact for candidate in plan.candidates] == [big, small], "the control: both planned"
        outcome = self._evict_planting_after_the_first_delete(
            plan,
            lambda: self._process_working_in(later / "src", pid="4243"),
        )

        assert not big.exists(), "the control: candidate #1 was reclaimed"
        assert small.exists(), "an agent that arrived mid-batch must not have the floor pulled out"
        assert any("a live process is working inside the checkout" in line for line in outcome.skipped), outcome.skipped

    def test_a_link_planted_mid_batch_still_protects_its_target(self) -> None:
        big, _later, shared = self._two_candidates("clone")
        worktree = make_git_repo(self.workspace / "wt-9999")

        plan = self._plan()
        assert [candidate.artifact for candidate in plan.candidates] == [big, shared], "the control: both planned"
        outcome = self._evict_planting_after_the_first_delete(
            plan,
            lambda: (worktree / "node_modules").symlink_to(shared),
        )

        assert not big.exists(), "the control: candidate #1 was reclaimed"
        assert shared.exists(), "a link planted mid-batch must be honoured before the NEXT delete"
        assert any(str(shared) in line for line in outcome.skipped), outcome.skipped

    def test_a_writer_after_the_final_scan_cannot_leave_a_dangling_link(self) -> None:
        """A writer arriving after the final scan cannot leave a dangling dependency."""
        clone = make_git_repo(self.workspace / "clone")
        shared = _artifact_in(clone, name="node_modules")
        worktree = make_git_repo(self.workspace / "wt-created-during-delete")
        self._some_process_exists()
        plan = self._plan()
        real_remove = artifact_eviction._remove_anchored_candidate
        writer_started = Event()
        writer_finished = Event()
        writers: list[Thread] = []

        def _plant_dependency() -> None:
            writer_started.set()
            with artifact_source_lock(shared, blocking=True) as locked:
                if locked and shared.is_dir():
                    (worktree / "node_modules").symlink_to(shared)
            writer_finished.set()

        def _plant_then_remove(candidate: ArtifactCandidate) -> str:
            writer = Thread(target=_plant_dependency)
            writers.append(writer)
            writer.start()
            assert writer_started.wait(timeout=1)
            writer_finished.wait(timeout=0.1)
            return real_remove(candidate)

        with patch.object(artifact_eviction, "_remove_anchored_candidate", side_effect=_plant_then_remove):
            outcome = evict_artifacts(plan)

        for writer in writers:
            writer.join(timeout=1)
        link = worktree / "node_modules"
        assert not link.is_symlink(), "a writer that lost the lock race must not leave a dangling dependency"
        assert not shared.exists(), "the deletion that acquired the artifact lock first may complete"
        assert outcome.evicted == (str(shared),)

    def test_an_artifact_locked_by_provisioning_is_skipped(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout, name="node_modules")
        self._some_process_exists()
        plan = self._plan()

        with artifact_source_lock(artifact, blocking=True) as locked:
            assert locked
            outcome = evict_artifacts(plan)

        assert artifact.exists()
        assert outcome.evicted == ()
        assert any("busy or could not be locked" in line for line in outcome.skipped)

    def test_a_checkout_created_mid_batch_can_protect_the_next_target(self) -> None:
        big, _later, shared = self._two_candidates("clone")

        def _create_checkout_and_link() -> None:
            worktree = make_git_repo(self.workspace / "wt-created-after-plan")
            (worktree / "node_modules").symlink_to(shared)

        plan = self._plan()
        assert [candidate.artifact for candidate in plan.candidates] == [big, shared]
        outcome = self._evict_planting_after_the_first_delete(plan, _create_checkout_and_link)

        assert not big.exists()
        assert shared.exists()
        assert any(str(shared) in line for line in outcome.skipped)

    def test_an_enumeration_gap_opening_mid_batch_stops_remaining_deletes(self) -> None:
        big, _later, small = self._two_candidates("later")
        plan = self._plan()
        registry = CheckoutRegistry(
            frozenset(str(path) for path in plan.checkouts),
            ("could not scan a new checkout root",),
        )

        def _open_gap() -> None:
            patcher = patch(f"{_REGISTRY}.scan_checkout_paths", return_value=registry)
            patcher.start()
            self.addCleanup(patcher.stop)

        outcome = self._evict_planting_after_the_first_delete(plan, _open_gap)

        assert not big.exists()
        assert small.exists()
        assert outcome.refusal


class TestCap(_EvictionFixture):
    def test_the_cap_spends_its_budget_on_the_largest_and_reports_what_it_deferred(self) -> None:
        """First-come selection let a pass burn its whole budget on kilobyte candidates."""
        checkout = make_git_repo(self.workspace / "clone")
        sizes = {".venv": 1_000, HOOK_ENV_NAME: 5_000, "node_modules": 9_000, ".nx": 3_000, ".angular": 7_000}
        artifacts = {name: _artifact_in(checkout, name=name, size=size) for name, size in sizes.items()}
        _age(checkout)
        self._some_process_exists()

        with patch.object(artifact_eviction, "_MAX_EVICTIONS_PER_PASS", 2):
            plan = self._plan()

        assert [candidate.artifact for candidate in plan.candidates] == [
            artifacts["node_modules"],
            artifacts[".angular"],
        ], "the two largest, in descending size order"
        assert sum("per-pass cap" in line for line in plan.deferred) == 3
        assert plan.kept == (), "a cap deferral is not a safety keep"


class TestThePassIsNotItsOwnWitness(_EvictionFixture):
    """A reaper never counts its own descriptors as somebody else's work.

    ``artifact_source_lock`` opens the candidate as a directory fd and keeps it
    through the delete-time liveness read, so a table that folds THIS process's
    ``fd`` entries in answers "a live process is working inside the checkout" for
    every candidate — a guard stop that reads as correct while the pass reclaims
    nothing on every host whose table is readable at all.
    """

    def test_this_passs_own_lock_descriptor_does_not_keep_the_artifact(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout)
        self._some_process_exists()
        holding(self.this_process, artifact)

        outcome = evict_artifacts(self._plan())

        assert not artifact.exists(), "the pass read its own deletion lock as another process's work"
        assert outcome.evicted == (str(artifact),)

    def test_another_process_holding_the_same_descriptor_still_keeps_it(self) -> None:
        """The control: an fd on the artifact is decisive — it is only WHOSE fd that differs."""
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout)
        self._some_process_exists()
        holding(self.host_proc / "7777", artifact)

        plan = self._plan()
        evict_artifacts(plan)

        assert artifact.exists()
        assert any("a live process is working inside the checkout" in line for line in plan.kept)

    def test_a_table_that_will_not_say_which_process_this_is_refuses_the_pass(self) -> None:
        """Without that answer this pass's own descriptors are indistinguishable from anyone's."""
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout)
        self._some_process_exists()
        (self.host_proc / "self").unlink()

        plan = self._plan()

        assert plan.refusal
        assert plan.candidates == ()
        assert artifact.exists()


class TestAMutePidCostsReclaimRatherThanTheWholePass(_EvictionFixture):
    """Only a pid's own uid may read its links, so a shared box always has mute pids.

    214 of 304 pids on the box that produced this issue are root-owned and 35 of 325
    present a listable-but-unresolvable ``fd``. A table that demanded every answer
    would refuse forever — the pass, the ``clean-all`` liveness guard and the
    worktree GC alike.
    """

    def test_a_pid_this_uid_cannot_read_narrows_the_answer_without_refusing(self) -> None:
        checkout = make_git_repo(self.workspace / "clone")
        artifact = _artifact_in(checkout)
        self._some_process_exists()
        mute = self.host_proc / "12" / "fd"
        mute.mkdir(parents=True)
        (mute / "0").write_bytes(b"")  # listable, unresolvable: the shape 35 of 325 host pids present

        plan = self._plan()
        outcome = evict_artifacts(plan)

        assert not plan.refusal, "a mute pid narrows what is known; it does not refuse the pass"
        assert not artifact.exists()
        assert outcome.freed_bytes > 0
