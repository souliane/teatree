"""In-process unit coverage for the fleet-claim primitives + wire resolvers.

The multiprocessing race in ``test_claim_ref`` proves the concurrency contract but
runs in forked children (invisible to coverage); these single-process tests
exercise the remaining branches — ``release``, the fail-safe error paths, the
metadata parser, and the Django-side wiring — directly.
"""

import os
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from teatree.core.fleet import claim as fleet_claim
from teatree.core.fleet import wire as fleet_claim_wire
from teatree.core.fleet.wire import host_from_issue_url
from teatree.instance_id import instance_id

from ._git_origin import (
    FIXTURE_IDENTITY,
    as_other_instance,
    as_twin_instance,
    git,
    init_bare,
    init_client,
    init_with_origin,
    private_origin,
    ref_sha,
    run_before_upload_pack,
)

_KEY = "https://github.com/souliane/teatree/issues/99"


def _main_with_a_root_and_a_child(tmp_path: Path, bare: Path) -> str:
    """Replace *bare*'s ``main`` with a root holding a file plus one child, and return the root."""
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", seed.name)
    (seed / "README").write_text("root\n", encoding="utf-8")
    git(seed, "add", "README")
    git(seed, *FIXTURE_IDENTITY, "commit", "-q", "-m", "root")
    root = git(seed, "rev-parse", "HEAD")
    (seed / "README").write_text("child\n", encoding="utf-8")
    git(seed, *FIXTURE_IDENTITY, "commit", "-q", "-am", "child")
    git(seed, "push", "-q", "--force", str(bare), "main:main")
    return root


def _rewritten_away_history(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "rewritten-away"
    git(tmp_path, "init", "-q", "-b", "main", repo.name)
    (repo / "ONLY_IN_THE_OLD_HISTORY").write_text("old\n", encoding="utf-8")
    git(repo, "add", "ONLY_IN_THE_OLD_HISTORY")
    git(repo, *FIXTURE_IDENTITY, "commit", "-q", "-m", "old root")
    return repo, git(repo, "rev-parse", "HEAD")


def _with_the_old_history_fetched(holder: Path, rewritten_away: Path) -> Path:
    git(holder, "fetch", "-q", "--no-tags", str(rewritten_away), "main:refs/heads/old-history")
    return holder


class TestClaimCommitShape:
    def test_each_claim_commit_has_the_default_branch_root_as_its_only_parent_and_its_tree(
        self, tmp_path, subtests
    ) -> None:
        bare = init_bare(tmp_path / "o.git")
        root = _main_with_a_root_and_a_child(tmp_path, bare)
        holder = init_client(tmp_path / "holder", bare)
        acquired = fleet_claim.acquire(_KEY, repo=str(holder), ttl_seconds=10.0, now=1000.0)
        assert acquired is not None
        beat = fleet_claim.heartbeat(acquired, repo=str(holder), now=1001.0)
        assert isinstance(beat, fleet_claim.Claim)
        with as_other_instance(tmp_path / "rival-data"):
            stolen = fleet_claim.steal_if_expired(
                _KEY, repo=str(init_client(tmp_path / "rival", bare)), ttl_seconds=10.0, now=5000.0
            )
        assert stolen is not None
        assert git(bare, "rev-parse", "main") != root

        for operation, written in (("acquire", acquired), ("heartbeat", beat), ("steal", stolen)):
            with subtests.test(operation=operation):
                assert git(bare, "rev-list", "--parents", "-n", "1", written.sha).split()[1:] == [root]
                assert git(bare, "rev-parse", f"{written.sha}^{{tree}}") == git(bare, "rev-parse", f"{root}^{{tree}}")

    def test_a_clone_whose_tracking_ref_predates_a_history_rewrite_descends_from_the_root_origin_holds(
        self, tmp_path: Path
    ) -> None:
        bare = init_bare(tmp_path / "o.git")
        rewritten_away, old_root = _rewritten_away_history(tmp_path)
        holder = init_client(tmp_path / "holder", rewritten_away)
        git(holder, "remote", "set-url", "origin", f"file://{bare}")

        claim = fleet_claim.acquire(_KEY, repo=str(holder))

        assert claim is not None
        assert git(bare, "rev-list", "--parents", "-n", "1", claim.sha).split()[1:] == [git(bare, "rev-parse", "main")]
        with pytest.raises(subprocess.CalledProcessError):
            git(bare, "cat-file", "-e", old_root)

    def test_a_replace_graft_in_the_clone_never_becomes_the_claim_commits_parent(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        published_root = git(bare, "rev-parse", "main")
        rewritten_away, old_root = _rewritten_away_history(tmp_path)
        holder = _with_the_old_history_fetched(init_client(tmp_path / "holder", bare), rewritten_away)
        git(holder, "replace", "--graft", published_root, old_root)

        claim = fleet_claim.acquire(_KEY, repo=str(holder))

        assert claim is not None
        assert git(bare, "rev-list", "--parents", "-n", "1", claim.sha).split()[1:] == [published_root]
        with pytest.raises(subprocess.CalledProcessError):
            git(bare, "cat-file", "-e", old_root)

    def test_a_replace_ref_for_the_root_never_lends_the_claim_commit_its_tree(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        published_root = git(bare, "rev-parse", "main")
        rewritten_away, old_root = _rewritten_away_history(tmp_path)
        old_tree = git(rewritten_away, "rev-parse", "HEAD^{tree}")
        holder = _with_the_old_history_fetched(init_client(tmp_path / "holder", bare), rewritten_away)
        git(holder, "replace", published_root, old_root)

        claim = fleet_claim.acquire(_KEY, repo=str(holder))

        assert claim is not None
        assert git(bare, "rev-parse", f"{claim.sha}^{{tree}}") == git(bare, "rev-parse", f"{published_root}^{{tree}}")
        with pytest.raises(subprocess.CalledProcessError):
            git(bare, "cat-file", "-e", old_tree)

    def test_a_clone_that_never_fetched_claims_on_the_root_origin_holds(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        never_fetched = init_with_origin(tmp_path / "c", f"file://{bare}")

        claim = fleet_claim.acquire(_KEY, repo=str(never_fetched))

        assert claim is not None
        assert git(bare, "rev-list", "--parents", "-n", "1", claim.sha).split()[1:] == [git(bare, "rev-parse", "main")]

    def test_a_clone_without_origin_head_claims_on_a_default_branch_of_any_name(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        root = git(bare, "rev-parse", "main")
        git(bare, "update-ref", "refs/heads/trunk", root)
        git(bare, "symbolic-ref", "HEAD", "refs/heads/trunk")
        git(bare, "update-ref", "-d", "refs/heads/main")
        holder = init_client(tmp_path / "holder", bare)
        git(holder, "remote", "set-head", "origin", "--delete")
        assert git(holder, "for-each-ref", "--format=%(refname)") == "refs/remotes/origin/trunk"
        merged_since_the_fetch = git(bare, *FIXTURE_IDENTITY, "commit-tree", f"{root}^{{tree}}", "-p", root, "-m", "m")
        git(bare, "update-ref", "refs/heads/trunk", merged_since_the_fetch)

        claim = fleet_claim.acquire(_KEY, repo=str(holder))

        assert claim is not None
        assert git(bare, "rev-list", "--parents", "-n", "1", claim.sha).split()[1:] == [root]


class TestClaimRef:
    def test_valid_key_is_a_ref_with_slug_and_hash(self) -> None:
        ref = fleet_claim.claim_ref(_KEY)
        assert ref.startswith("refs/teatree/claims/")
        assert ref == fleet_claim.claim_ref(_KEY)  # deterministic

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            fleet_claim.claim_ref("")

    def test_symbol_only_key_degrades_to_hash_only(self) -> None:
        ref = fleet_claim.claim_ref("///:::")  # sanitizes to empty slug
        assert ref.startswith("refs/teatree/claims/")
        assert ":" not in ref
        assert " " not in ref


class TestReleaseAndFence:
    def test_release_deletes_the_ref_and_frees_it(self, tmp_path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        fleet_claim.release(claim, repo=str(client))
        assert fleet_claim.read_tip(_KEY, repo=str(client)) is None
        # Freed: a fresh acquire succeeds.
        again = fleet_claim.acquire(_KEY, repo=str(client))
        assert again is not None

    def test_stale_release_never_removes_a_rivals_claim(self, tmp_path) -> None:
        bare = init_bare(tmp_path / "o.git")
        a = init_client(tmp_path / "a", bare)
        b = init_client(tmp_path / "b", bare)
        original = fleet_claim.acquire(_KEY, repo=str(a), ttl_seconds=10.0, now=1000.0)
        stolen = fleet_claim.steal_if_expired(_KEY, repo=str(b), ttl_seconds=10.0, now=5000.0)
        assert original is not None
        assert stolen is not None
        # A releases its OWN (stale) claim: the CAS lease mismatches, so it is a
        # no-op — the thief's live claim survives.
        fleet_claim.release(original, repo=str(a))
        assert ref_sha(bare, original.ref) == stolen.sha

    def test_read_tip_is_none_when_ref_absent(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        assert fleet_claim.read_tip(_KEY, repo=str(client)) is None

    def test_read_tip_names_the_instance_that_wrote_the_claim(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        tip = fleet_claim.read_tip(_KEY, repo=str(client))
        assert tip is not None
        assert tip.sha == claim.sha
        assert tip.held_by(instance_id(), claim.lineage)
        assert not tip.held_by("", claim.lineage)
        assert not tip.held_by(instance_id(), "")
        with as_other_instance(tmp_path / "rival-data"):
            assert not tip.held_by(instance_id(), claim.lineage)

    def test_heartbeat_moves_the_ref_and_stays_held(self, tmp_path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        beat = fleet_claim.heartbeat(claim, repo=str(client))
        assert isinstance(beat, fleet_claim.Claim)
        assert beat.sha != claim.sha
        assert ref_sha(bare, claim.ref) == beat.sha

    def test_heartbeat_adopts_a_tip_this_instance_wrote_since_its_recorded_sha(self, tmp_path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        unrecorded = fleet_claim.heartbeat(claim, repo=str(client))
        assert isinstance(unrecorded, fleet_claim.Claim)

        beat = fleet_claim.heartbeat(claim, repo=str(client))

        assert isinstance(beat, fleet_claim.Claim)
        assert beat.sha == ref_sha(bare, claim.ref) == unrecorded.sha

    def test_heartbeat_does_not_adopt_a_tip_another_instance_wrote_under_this_lineage(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        holder = init_client(tmp_path / "holder", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(holder))
        assert claim is not None
        with as_other_instance(tmp_path / "rival-data"):
            rival = fleet_claim.heartbeat(
                replace(claim, instance_id=instance_id()), repo=str(init_client(tmp_path / "rival", bare))
            )
        assert isinstance(rival, fleet_claim.Claim)
        assert rival.lineage == claim.lineage
        assert rival.instance_id != claim.instance_id

        lost = fleet_claim.heartbeat(claim, repo=str(holder))

        assert isinstance(lost, fleet_claim.ClaimLost)
        assert lost.observed_sha == rival.sha

    def test_heartbeat_does_not_adopt_a_twin_tip(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        holder = init_client(tmp_path / "holder", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(holder), ttl_seconds=10.0, now=1000.0)
        assert claim is not None
        with as_twin_instance(tmp_path / "twin-data"):
            twin = fleet_claim.steal_if_expired(
                _KEY, repo=str(init_client(tmp_path / "twin", bare)), ttl_seconds=10.0, now=5000.0
            )
        assert twin is not None
        assert twin.instance_id == claim.instance_id

        lost = fleet_claim.heartbeat(claim, repo=str(holder), now=5001.0)

        assert isinstance(lost, fleet_claim.ClaimLost)
        assert lost.observed_sha == twin.sha


class TestClaimTipIdentity:
    _ID = "0d9f3c1e-6b2a-4c8e-9f1a-2b3c4d5e6f70"
    _LINEAGE = "4f1c2b3a5d6e7f8091a2b3c4d5e6f708"

    @staticmethod
    def _tip(**meta: object) -> fleet_claim.ClaimTip:
        return fleet_claim.ClaimTip(sha="a" * 40, meta=cast("fleet_claim.ClaimMeta", meta))

    def test_the_recorded_pair_holds_the_claim(self) -> None:
        assert self._tip(instance_id=self._ID, lineage=self._LINEAGE).held_by(self._ID, self._LINEAGE)

    def test_id_and_lineage_are_each_compared_verbatim(self, subtests) -> None:
        near_misses = {
            "case": (self._ID.upper(), self._LINEAGE.upper()),
            "prefix": (self._ID[:8], self._LINEAGE[:8]),
            "suffix": (self._ID + "x", self._LINEAGE + "x"),
            "space": (" " + self._ID, " " + self._LINEAGE),
        }
        for name, (near_id, near_lineage) in near_misses.items():
            with subtests.test(near=name):
                assert not self._tip(instance_id=near_id, lineage=self._LINEAGE).held_by(self._ID, self._LINEAGE)
                assert not self._tip(instance_id=self._ID, lineage=near_lineage).held_by(self._ID, self._LINEAGE)

    def test_an_empty_id_or_lineage_never_matches(self) -> None:
        assert not self._tip(instance_id="", lineage="").held_by("", "")
        assert not self._tip(instance_id=self._ID, lineage="").held_by(self._ID, "")
        assert not self._tip(instance_id="", lineage=self._LINEAGE).held_by("", self._LINEAGE)

    def test_a_non_string_lineage_reads_as_none(self, subtests) -> None:
        for value, claimed in ((0, "0"), (None, "None"), (["L"], "L"), (123, "123")):
            with subtests.test(value=value):
                tip = self._tip(instance_id=self._ID, lineage=value)
                assert tip.lineage == ""
                assert not tip.held_by(self._ID, claimed)

    def test_a_record_with_trailing_data_or_a_non_object_is_unreadable(self) -> None:
        assert fleet_claim._parse_meta(f'{{"instance_id": "{self._ID}"}} trailing') is None
        assert fleet_claim._parse_meta(f'["{self._ID}"]') is None


class TestReadTip:
    def test_a_ref_deleted_mid_read_reads_as_absent(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        run_before_upload_pack(client, {2: f'git --git-dir="{bare}" update-ref -d {claim.ref}'})

        assert fleet_claim.read_tip(_KEY, repo=str(client)) is None

    def test_a_ref_re_pointed_mid_read_is_read_again(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        beat = fleet_claim.heartbeat(claim, repo=str(client))
        assert isinstance(beat, fleet_claim.Claim)
        git(bare, "update-ref", claim.ref, claim.sha)
        run_before_upload_pack(client, {2: f'git --git-dir="{bare}" update-ref {claim.ref} {beat.sha}'})

        tip = fleet_claim.read_tip(_KEY, repo=str(client))

        assert tip is not None
        assert tip.sha == beat.sha
        assert tip.lineage == claim.lineage != ""

    def test_a_ref_that_moves_away_and_back_mid_read_reads_as_unreadable(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        beat = fleet_claim.heartbeat(claim, repo=str(client))
        assert isinstance(beat, fleet_claim.Claim)
        git(bare, "update-ref", claim.ref, claim.sha)
        away = f'git --git-dir="{bare}" update-ref {claim.ref} {beat.sha}'
        back = f'git --git-dir="{bare}" update-ref {claim.ref} {claim.sha}'
        run_before_upload_pack(client, {2: away, 3: back})

        assert fleet_claim.read_tip(_KEY, repo=str(client)) == fleet_claim.ClaimTip(sha=claim.sha, meta=None)

    def test_a_ref_that_keeps_moving_through_every_read_fails_safe(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        first = fleet_claim.heartbeat(claim, repo=str(client))
        assert isinstance(first, fleet_claim.Claim)
        second = fleet_claim.heartbeat(first, repo=str(client))
        assert isinstance(second, fleet_claim.Claim)
        git(bare, "update-ref", claim.ref, claim.sha)
        run_before_upload_pack(
            client,
            {
                2: f'git --git-dir="{bare}" update-ref {claim.ref} {first.sha}',
                4: f'git --git-dir="{bare}" update-ref {claim.ref} {second.sha}',
            },
        )

        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="kept moving"):
            fleet_claim.read_tip(_KEY, repo=str(client))


class TestLineage:
    def test_steal_mints_a_new_lineage_and_heartbeat_carries_it_forward(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        holder = init_client(tmp_path / "holder", bare)
        acquired = fleet_claim.acquire(_KEY, repo=str(holder), ttl_seconds=10.0, now=1000.0)
        assert acquired is not None
        assert acquired.lineage

        beat = fleet_claim.heartbeat(acquired, repo=str(holder), now=1001.0)
        assert isinstance(beat, fleet_claim.Claim)
        after_beat = fleet_claim.read_tip(_KEY, repo=str(holder))
        assert after_beat is not None
        assert after_beat.sha == beat.sha
        assert after_beat.lineage == beat.lineage == acquired.lineage

        with as_other_instance(tmp_path / "rival-data"):
            stolen = fleet_claim.steal_if_expired(
                _KEY, repo=str(init_client(tmp_path / "rival", bare)), ttl_seconds=10.0, now=5000.0
            )
        assert stolen is not None
        after_steal = fleet_claim.read_tip(_KEY, repo=str(holder))
        assert after_steal is not None
        assert after_steal.lineage == stolen.lineage
        assert stolen.lineage not in {"", acquired.lineage}


def _git_on_path(tmp_path: Path, subcommand: str, *, then: str) -> str:
    shim = tmp_path / "git-shim" / "git"
    shim.parent.mkdir()
    stall = f'for arg; do [ "$arg" = "{subcommand}" ] && {then}; done'
    shim.write_text(f'#!/bin/sh\n{stall}\nexec "{shutil.which("git")}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return str(shim.parent)


def _claim_and_read_the_tip(repo: Path) -> None:
    fleet_claim.acquire(_KEY, repo=str(repo))
    fleet_claim.read_tip(_KEY, repo=str(repo))


def _running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(")")[2].split()[0]
    except FileNotFoundError:
        return False
    return state not in {"Z", "X"}


def _ends_within(pid: int, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _running(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def _reject_pushes(bare) -> None:
    """Install a ``pre-receive`` hook that refuses every push (push denied, reads OK)."""
    hook = Path(bare) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


class TestErrorPaths:
    def test_acquire_raises_when_remote_unreachable(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", tmp_path / "absent.git")  # origin does not exist
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.acquire(_KEY, repo=str(client))

    def test_acquire_raises_on_local_git_failure(self, tmp_path) -> None:
        # N4: a directory that is not a repo surfaces as FleetClaimUnavailableError
        # (the module contract), never a bare CommandFailedError.
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.acquire(_KEY, repo=str(not_a_repo))

    def test_acquire_fails_safe_when_origin_advertises_no_default_branch(self, tmp_path: Path, subtests) -> None:
        unborn = tmp_path / "unborn.git"
        git(tmp_path, "init", "--bare", "-q", unborn.name)
        detached = init_bare(tmp_path / "detached.git")
        git(detached, "update-ref", "--no-deref", "HEAD", git(detached, "rev-parse", "main"))
        for bare in (unborn, detached):
            with subtests.test(origin=bare.name):
                client = init_client(tmp_path / f"client-of-{bare.name}", bare)
                with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="advertises no default branch"):
                    fleet_claim.acquire(_KEY, repo=str(client))
                assert ref_sha(bare, fleet_claim.claim_ref(_KEY)) == ""

    def test_an_unreadable_default_branch_names_gits_cause_without_the_credential(self, tmp_path: Path) -> None:
        bare, client = private_origin(tmp_path, "routed-token")
        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="credential refused: <redacted>") as refused:
            fleet_claim.acquire(_KEY, repo=str(client), extra_env={"GH_TOKEN": "stale-token"})
        assert "stale-token" not in str(refused.value)
        assert ref_sha(bare, fleet_claim.claim_ref(_KEY)) == ""

    def test_acquire_fails_safe_when_the_default_branch_cannot_be_fetched(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        never_fetched = init_with_origin(tmp_path / "c", f"file://{bare}")
        run_before_upload_pack(never_fetched, {2: "exit 1"})
        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="fetching it failed"):
            fleet_claim.acquire(_KEY, repo=str(never_fetched))
        assert ref_sha(bare, fleet_claim.claim_ref(_KEY)) == ""

    def test_acquire_raises_when_push_denied_but_ref_absent(self, tmp_path) -> None:
        # ls-remote succeeds (ref absent) yet the create push is refused — a
        # read-only / permission-denied remote. Fail safe: raise, never a silent
        # "won".
        bare = init_bare(tmp_path / "o.git")
        _reject_pushes(bare)
        client = init_client(tmp_path / "c", bare)
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.acquire(_KEY, repo=str(client))

    def test_a_refusing_pre_push_hook_says_why(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        hook = client / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho 'gate refused: non-noreply identity'\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="gate refused: non-noreply identity"):
            fleet_claim.acquire(_KEY, repo=str(client))

    def test_a_refused_heartbeat_that_leaves_the_ref_on_our_sha_is_an_outage_not_a_loss(self, tmp_path: Path) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        claim = fleet_claim.acquire(_KEY, repo=str(client))
        assert claim is not None
        hook = client / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho 'gate refused the beat'\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="still holds our sha: gate refused the beat"):
            fleet_claim.heartbeat(claim, repo=str(client))

        assert ref_sha(bare, claim.ref) == claim.sha

    def test_a_claim_commit_git_cannot_write_fails_safe_and_leaves_a_rivals_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare = init_bare(tmp_path / "o.git")
        with as_other_instance(tmp_path / "rival-data"):
            rival = fleet_claim.acquire(_KEY, repo=str(init_client(tmp_path / "rival", bare)))
        assert rival is not None
        client = init_client(tmp_path / "c", bare)
        monkeypatch.setenv("PATH", f"{_git_on_path(tmp_path, 'commit-tree', then='exit 1')}:{os.environ['PATH']}")

        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="cannot write the claim commit"):
            fleet_claim.acquire(_KEY, repo=str(client))

        assert ref_sha(bare, rival.ref) == rival.sha

    @pytest.mark.parametrize(
        "subcommand", ["rev-parse", "ls-remote", "rev-list", "fetch", "commit-tree", "push", "log"]
    )
    def test_every_claim_git_call_fails_safe_at_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subcommand: str
    ) -> None:
        never_fetched = init_with_origin(tmp_path / "c", f"file://{init_bare(tmp_path / 'o.git')}")
        if subcommand == "log":
            assert fleet_claim.acquire(_KEY, repo=str(never_fetched)) is not None
        monkeypatch.setenv("PATH", f"{_git_on_path(tmp_path, subcommand, then='exec sleep 20')}:{os.environ['PATH']}")
        monkeypatch.setattr(fleet_claim, "_GIT_TIMEOUT_SECONDS", 2.0)
        stalled = rf"`git [^`]*\b{re.escape(subcommand)}\b[^`]*` in \S+ did not finish within 2s"

        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match=stalled) as refused:
            _claim_and_read_the_tip(never_fetched)

        assert '"work_key"' not in str(refused.value)

    def test_a_claim_push_whose_hook_outlives_the_deadline_fails_safe_and_ends_the_hook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        hook_child_pid = tmp_path / "hook-child.pid"
        hook = client / ".git" / "hooks" / "pre-push"
        hook.write_text(f'#!/bin/sh\nsleep 40 &\necho $! > "{hook_child_pid}"\nwait\n', encoding="utf-8")
        hook.chmod(0o755)
        monkeypatch.setattr(fleet_claim, "_GIT_TIMEOUT_SECONDS", 5.0)
        started = time.monotonic()

        with pytest.raises(fleet_claim.FleetClaimUnavailableError, match="did not finish within 5s"):
            fleet_claim.acquire(_KEY, repo=str(client))

        assert time.monotonic() - started < 30
        assert ref_sha(bare, fleet_claim.claim_ref(_KEY)) == ""
        assert _ends_within(int(hook_child_pid.read_text(encoding="utf-8")), seconds=10.0)

    def test_steal_returns_none_when_cas_is_lost(self, tmp_path, monkeypatch) -> None:
        # The ref is expired (so the steal is attempted) but the CAS is rejected —
        # a concurrent stealer won the race first. This instance stands down.
        bare = init_bare(tmp_path / "o.git")
        a = init_client(tmp_path / "a", bare)
        b = init_client(tmp_path / "b", bare)
        fleet_claim.acquire(_KEY, repo=str(a), ttl_seconds=10.0, now=1000.0)

        def rival_steals_first(_scope, ref: str, **_kwargs) -> subprocess.CompletedProcess[str]:
            tree = git(bare, "hash-object", "-t", "tree", "-w", "/dev/null").strip()
            rival = git(
                bare,
                "-c",
                "user.name=rival",
                "-c",
                "user.email=rival@example.invalid",
                "commit-tree",
                tree,
                "-m",
                "rival claim",
            ).strip()
            git(bare, "update-ref", ref, rival)
            return subprocess.CompletedProcess(args=["git", "push"], returncode=1, stdout="", stderr="stale lease")

        monkeypatch.setattr(fleet_claim, "_cas", rival_steals_first)
        assert fleet_claim.steal_if_expired(_KEY, repo=str(b), ttl_seconds=10.0, now=5000.0) is None

    def test_steal_raises_when_the_tip_object_is_unfetchable(self, tmp_path, monkeypatch) -> None:
        # The ref exists (ls-remote OK) but fetching its object fails — a corrupt
        # or unreachable remote mid-read. Fail safe: raise rather than mis-decide
        # liveness.
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        fleet_claim.acquire(_KEY, repo=str(client), ttl_seconds=10.0, now=1000.0)
        real_git = fleet_claim._git

        def fail_fetch(repo, args, *, env=None):
            if "fetch" in args:
                return real_git(repo, ["ls-remote", "does-not-exist"])  # non-zero exit
            return real_git(repo, args, env=env)

        monkeypatch.setattr(fleet_claim, "_git", fail_fetch)
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.steal_if_expired(_KEY, repo=str(client), ttl_seconds=10.0, now=5000.0)

    def test_steal_returns_none_when_ref_absent(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        assert fleet_claim.steal_if_expired(_KEY, repo=str(client), now=1e12) is None

    def test_steal_skips_a_claim_with_unreadable_metadata(self, tmp_path) -> None:
        # A ref whose commit message is not the JSON metadata: liveness cannot be
        # parsed, so it is treated as NOT expired and never stolen (fail safe).
        from ._git_origin import git  # noqa: PLC0415 — test-local

        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        tree = git(client, "mktree")
        sha = git(client, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "not json")
        git(client, "push", "origin", f"{sha}:{fleet_claim.claim_ref(_KEY)}")
        assert fleet_claim.steal_if_expired(_KEY, repo=str(client), now=1e12) is None


class TestMetadataHelpers:
    def test_parse_meta_tolerates_garbage(self) -> None:
        assert fleet_claim._parse_meta("") is None
        assert fleet_claim._parse_meta("not json") is None
        assert fleet_claim._parse_meta("[1, 2]") is None
        assert fleet_claim._parse_meta('{"a": 1}') == {"a": 1}

    def test_is_expired_reads_claimed_at_plus_ttl(self) -> None:
        # _parse_meta hands _is_expired whatever JSON was in the commit — possibly
        # partial/malformed — so these cast partial dicts model that reality.
        assert fleet_claim._is_expired(None, 100.0) is False
        live = cast("fleet_claim.ClaimMeta", {"claimed_at": 0.0, "ttl_seconds": 10.0})
        assert fleet_claim._is_expired(live, 5.0) is False
        assert fleet_claim._is_expired(live, 20.0) is True
        assert fleet_claim._is_expired(cast("fleet_claim.ClaimMeta", {"claimed_at": "x"}), 5.0) is False


class TestWireResolvers:
    def test_repo_name_from_github_issue_url(self) -> None:
        assert fleet_claim_wire.repo_name_from_issue_url("https://github.com/souliane/teatree/issues/42") == "teatree"

    def test_repo_name_from_gitlab_issue_url(self) -> None:
        assert fleet_claim_wire.repo_name_from_issue_url("https://gitlab.com/grp/sub/proj/-/issues/7") == "proj"

    def test_repo_name_from_pull_url(self) -> None:
        assert fleet_claim_wire.repo_name_from_issue_url("https://github.com/o/widget/pull/9") == "widget"

    def test_repo_name_none_when_no_marker(self) -> None:
        assert fleet_claim_wire.repo_name_from_issue_url("https://example.com/nothing-here") == ""

    def test_owner_repo_from_github_and_gitlab(self) -> None:
        assert fleet_claim_wire.owner_repo_from_issue_url(_KEY) == "souliane/teatree"
        assert (
            fleet_claim_wire.owner_repo_from_issue_url("https://gitlab.com/grp/sub/proj/-/issues/7") == "grp/sub/proj"
        )
        assert fleet_claim_wire.owner_repo_from_issue_url("https://example.com/nothing") == ""

    def test_a_work_item_url_resolves_like_the_issue_url_it_aliases(self) -> None:
        """The alias a forge hands out for a work item — the SAME object, one URL shape apart.

        A resolver that does not know it answers "" for the slug, so the claim mutex
        fails safe and refuses to start work on anything the forge names that way. Every
        other parser in this tree already folds the two spellings together; this one kept
        a private marker list that did not.
        """
        work_item = "https://gitlab.com/grp/sub/proj/-/work_items/7"
        issue = "https://gitlab.com/grp/sub/proj/-/issues/7"

        assert fleet_claim_wire.owner_repo_from_issue_url(work_item) == fleet_claim_wire.owner_repo_from_issue_url(
            issue
        )
        assert fleet_claim_wire.repo_name_from_issue_url(work_item) == fleet_claim_wire.repo_name_from_issue_url(issue)
        assert fleet_claim_wire.owner_repo_from_issue_url(work_item) == "grp/sub/proj"

    def test_a_merge_request_url_still_resolves(self) -> None:
        assert fleet_claim_wire.owner_repo_from_issue_url("https://gitlab.com/grp/proj/-/merge_requests/3") == (
            "grp/proj"
        )
        assert fleet_claim_wire.repo_name_from_issue_url("https://github.com/o/widget/pulls/9") == "widget"

    def test_a_trailing_slash_or_fragment_does_not_defeat_the_resolver(self) -> None:
        assert fleet_claim_wire.owner_repo_from_issue_url("https://gitlab.com/grp/proj/-/work_items/7/") == "grp/proj"
        assert (
            fleet_claim_wire.owner_repo_from_issue_url("https://github.com/souliane/teatree/issues/3009#directive=5")
            == "souliane/teatree"
        )

    def test_resolve_falls_back_to_t3_repo_env_when_origin_matches(self, tmp_path, monkeypatch) -> None:
        repo = init_with_origin(tmp_path / "teatree", "https://github.com/souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: None)
        monkeypatch.setenv("T3_REPO", str(repo))
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == str(repo)

    def test_resolve_empty_when_nothing_found(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: None)
        monkeypatch.delenv("T3_REPO", raising=False)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == ""

    def test_resolve_survives_a_clone_lookup_error(self, monkeypatch) -> None:
        def boom(*_):
            msg = "clone lookup blew up"
            raise RuntimeError(msg)

        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", boom)
        monkeypatch.delenv("T3_REPO", raising=False)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == ""  # suppressed -> fail safe

    def test_resolve_uses_clone_when_origin_hosts_the_issue(self, tmp_path, monkeypatch) -> None:
        clone = init_with_origin(tmp_path / "found", "git@github.com:souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == str(clone)

    def test_resolve_rejects_a_clone_whose_origin_hosts_a_different_repo(self, tmp_path, monkeypatch) -> None:
        # N1: a NAME-only match can resolve a clone whose origin points at the WRONG
        # forge repo; pushing the claim there would split the mutex across remotes.
        clone = init_with_origin(tmp_path / "found", "https://github.com/someone-else/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == ""  # origin mismatch -> fail safe

    def test_resolve_rejects_a_clone_with_no_origin(self, tmp_path, monkeypatch) -> None:
        clone = tmp_path / "no-origin"
        init_bare(clone)  # a bare repo has no origin remote
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == ""


class TestForgeHostIsPartOfTheIdentity:
    """The slug alone is not the repo — the same ``owner/repo`` exists on many forges.

    The claim ref is a server-side CAS on ONE receive-pack. Two instances pushing
    ``souliane/teatree`` claims to two different forges each win their own ref, so
    the mutex silently stops excluding anything and both implement the work item.
    """

    def test_same_slug_on_a_different_forge_is_rejected(self, tmp_path, monkeypatch) -> None:
        clone = init_with_origin(tmp_path / "found", "https://gitlab.com/souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == ""

    def test_the_matching_forge_is_still_accepted_over_ssh(self, tmp_path, monkeypatch) -> None:
        clone = init_with_origin(tmp_path / "found", "git@github.com:souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == str(clone)

    def test_a_port_and_case_difference_is_not_a_different_forge(self, tmp_path, monkeypatch) -> None:
        clone = init_with_origin(tmp_path / "found", "ssh://git@GitHub.com:22/souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == str(clone)

    def test_an_ssh_config_alias_resolves_to_its_real_hostname(self, tmp_path, monkeypatch) -> None:
        # `git@github-work:…` is one Host alias away from github.com. Comparing the
        # raw token would reject a perfectly valid work-account setup, so the alias
        # is resolved through the same `ssh -G` the transport itself would use.
        clone = init_with_origin(tmp_path / "found", "git@github-work:souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        monkeypatch.setattr(fleet_claim_wire, "_ssh_resolved_hostname", lambda host: "github.com")
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == str(clone)

    def test_an_unresolvable_alias_is_not_treated_as_a_conflict(self, tmp_path, monkeypatch) -> None:
        # No ssh on PATH, or an alias ssh cannot resolve: the host is UNKNOWN, not
        # different. Fall back to the slug-only verdict rather than refusing to
        # claim on a host we simply could not read.
        clone = init_with_origin(tmp_path / "found", "git@github-work:souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        monkeypatch.setattr(fleet_claim_wire, "_ssh_resolved_hostname", lambda host: "")
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == str(clone)

    def test_a_local_or_file_origin_is_unknown_not_different(self, tmp_path) -> None:
        # A clone-of-a-clone (and every local fixture) names no forge. Unknown must
        # not read as "different", or the host check would newly reject clones the
        # slug check accepts. Asserted on the predicate: a local origin never even
        # reaches it in practice, because `remote_slug` yields no owner/repo for one.
        for n, url in enumerate((f"file://{tmp_path}/souliane/teatree.git", f"{tmp_path}/souliane/teatree.git")):
            clone = init_with_origin(tmp_path / f"c{n}", url)
            assert fleet_claim_wire._forge_host_conflict(str(clone), _KEY) is False, url

    def test_the_issue_url_host_is_read_from_the_url_itself(self) -> None:
        assert host_from_issue_url(_KEY) == "github.com"
        assert host_from_issue_url("https://gitlab.example.org/g/s/p/-/issues/7") == "gitlab.example.org"
        assert host_from_issue_url("") == ""

    def test_an_unparsable_issue_url_is_never_a_conflict(self, tmp_path) -> None:
        clone = init_with_origin(tmp_path / "found", "https://gitlab.com/souliane/teatree.git")
        assert fleet_claim_wire._forge_host_conflict(str(clone), "not-a-url") is False

    def test_a_mirror_on_another_host_fails_safe_rather_than_splitting_the_mutex(self, tmp_path, monkeypatch) -> None:
        # A mirror carries the same content but a DIFFERENT receive-pack, so a claim
        # pushed there excludes nobody on the forge that owns the issue. Refusing to
        # claim degrades to local-only behaviour; accepting would manufacture a mutex
        # that does not exist.
        clone = init_with_origin(tmp_path / "found", "https://mirror.example.com/souliane/teatree.git")
        monkeypatch.setattr(fleet_claim_wire, "find_clone_path", lambda *_: clone)
        assert fleet_claim_wire.resolve_claim_repo(_KEY) == ""


class TestWireFailSafe:
    def test_acquire_issue_claim_no_repo_fails_safe(self, monkeypatch) -> None:
        monkeypatch.setattr(fleet_claim_wire, "resolve_claim_repo", lambda _: "")
        assert fleet_claim_wire.acquire_issue_claim(_KEY) is None

    def test_acquire_issue_claim_wins_and_returns_claim(self, tmp_path, monkeypatch) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        monkeypatch.setattr(fleet_claim_wire, "resolve_claim_repo", lambda _: str(client))
        claim = fleet_claim_wire.acquire_issue_claim(_KEY)
        assert claim is not None
        assert claim.sha
