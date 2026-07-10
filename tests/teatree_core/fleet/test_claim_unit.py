"""In-process unit coverage for the fleet-claim primitives + wire resolvers.

The multiprocessing race in ``test_claim_ref`` proves the concurrency contract but
runs in forked children (invisible to coverage); these single-process tests
exercise the remaining branches — ``release``, the fail-safe error paths, the
metadata parser, and the Django-side wiring — directly.
"""

from typing import cast

import pytest
from django.test import TestCase

from teatree.core import fleet_claim, fleet_claim_wire

from ._git_origin import init_bare, init_client, init_with_origin

_KEY = "https://github.com/souliane/teatree/issues/99"


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
        claim = fleet_claim.acquire(_KEY, repo=str(client), remote="origin")
        assert claim is not None
        fleet_claim.release(claim, repo=str(client), remote="origin")
        assert not fleet_claim.is_held_by_me(_KEY, claim, repo=str(client), remote="origin")
        # Freed: a fresh acquire succeeds.
        again = fleet_claim.acquire(_KEY, repo=str(client), remote="origin")
        assert again is not None

    def test_stale_release_never_removes_a_rivals_claim(self, tmp_path) -> None:
        bare = init_bare(tmp_path / "o.git")
        a = init_client(tmp_path / "a", bare)
        b = init_client(tmp_path / "b", bare)
        original = fleet_claim.acquire(_KEY, repo=str(a), remote="origin", ttl_seconds=10.0, now=1000.0)
        stolen = fleet_claim.steal_if_expired(_KEY, repo=str(b), remote="origin", ttl_seconds=10.0, now=5000.0)
        assert original is not None
        assert stolen is not None
        # A releases its OWN (stale) claim: the CAS lease mismatches, so it is a
        # no-op — the thief's live claim survives.
        fleet_claim.release(original, repo=str(a), remote="origin")
        assert fleet_claim.is_held_by_me(_KEY, stolen, repo=str(b), remote="origin")

    def test_is_held_by_me_is_false_for_empty_token(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        token = fleet_claim.Claim.from_token(_KEY, "")
        assert fleet_claim.is_held_by_me(_KEY, token, repo=str(client), remote="origin") is False

    def test_is_held_by_me_is_false_when_ref_absent(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        token = fleet_claim.Claim.from_token(_KEY, "deadbeef" * 5)
        assert fleet_claim.is_held_by_me(_KEY, token, repo=str(client), remote="origin") is False

    def test_heartbeat_moves_the_ref_and_stays_held(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        claim = fleet_claim.acquire(_KEY, repo=str(client), remote="origin")
        assert claim is not None
        beat = fleet_claim.heartbeat(claim, repo=str(client), remote="origin")
        assert isinstance(beat, fleet_claim.Claim)
        assert beat.sha != claim.sha
        assert fleet_claim.is_held_by_me(_KEY, beat, repo=str(client), remote="origin")


def _reject_pushes(bare) -> None:
    """Install a ``pre-receive`` hook that refuses every push (push denied, reads OK)."""
    from pathlib import Path  # noqa: PLC0415 — test-local

    hook = Path(bare) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


class TestErrorPaths:
    def test_acquire_raises_when_remote_unreachable(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", tmp_path / "absent.git")  # origin does not exist
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.acquire(_KEY, repo=str(client), remote="origin")

    def test_acquire_raises_on_local_git_failure(self, tmp_path) -> None:
        # N4: a repo where even the LOCAL mktree/commit-tree cannot run surfaces as
        # FleetClaimUnavailableError (the module contract), never a bare CommandFailedError.
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.acquire(_KEY, repo=str(not_a_repo), remote="origin")

    def test_acquire_raises_when_push_denied_but_ref_absent(self, tmp_path) -> None:
        # ls-remote succeeds (ref absent) yet the create push is refused — a
        # read-only / permission-denied remote. Fail safe: raise, never a silent
        # "won".
        bare = init_bare(tmp_path / "o.git")
        _reject_pushes(bare)
        client = init_client(tmp_path / "c", bare)
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.acquire(_KEY, repo=str(client), remote="origin")

    def test_steal_returns_none_when_cas_is_lost(self, tmp_path, monkeypatch) -> None:
        # The ref is expired (so the steal is attempted) but the CAS is rejected —
        # a concurrent stealer won the race first. This instance stands down.
        bare = init_bare(tmp_path / "o.git")
        a = init_client(tmp_path / "a", bare)
        b = init_client(tmp_path / "b", bare)
        fleet_claim.acquire(_KEY, repo=str(a), remote="origin", ttl_seconds=10.0, now=1000.0)
        monkeypatch.setattr(fleet_claim, "_cas", lambda *_, **__: False)
        assert fleet_claim.steal_if_expired(_KEY, repo=str(b), remote="origin", ttl_seconds=10.0, now=5000.0) is None

    def test_fetch_claim_raises_when_object_unfetchable(self, tmp_path, monkeypatch) -> None:
        # The ref exists (ls-remote OK) but fetching its object fails — a corrupt
        # or unreachable remote mid-read. Fail safe: raise rather than mis-decide
        # liveness.
        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        fleet_claim.acquire(_KEY, repo=str(client), remote="origin", ttl_seconds=10.0, now=1000.0)
        real_git = fleet_claim._git

        def fail_fetch(repo, args):
            if args and args[0] == "fetch":
                return real_git(repo, ["ls-remote", "does-not-exist"])  # non-zero exit
            return real_git(repo, args)

        monkeypatch.setattr(fleet_claim, "_git", fail_fetch)
        with pytest.raises(fleet_claim.FleetClaimUnavailableError):
            fleet_claim.steal_if_expired(_KEY, repo=str(client), remote="origin", ttl_seconds=10.0, now=5000.0)

    def test_steal_returns_none_when_ref_absent(self, tmp_path) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        assert fleet_claim.steal_if_expired(_KEY, repo=str(client), remote="origin", now=1e12) is None

    def test_steal_skips_a_claim_with_unreadable_metadata(self, tmp_path) -> None:
        # A ref whose commit message is not the JSON metadata: liveness cannot be
        # parsed, so it is treated as NOT expired and never stolen (fail safe).
        from ._git_origin import git  # noqa: PLC0415 — test-local

        bare = init_bare(tmp_path / "o.git")
        client = init_client(tmp_path / "c", bare)
        tree = git(client, "mktree")
        sha = git(client, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "not json")
        git(client, "push", "origin", f"{sha}:{fleet_claim.claim_ref(_KEY)}")
        assert fleet_claim.steal_if_expired(_KEY, repo=str(client), remote="origin", now=1e12) is None


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

    def test_still_held_empty_sha_is_false(self) -> None:
        assert fleet_claim_wire.issue_claim_still_held(_KEY, "", "/some/repo") is False

    def test_still_held_true_when_ref_matches(self, tmp_path, monkeypatch) -> None:
        client = init_client(tmp_path / "c", init_bare(tmp_path / "o.git"))
        monkeypatch.setattr(fleet_claim_wire, "resolve_claim_repo", lambda _: str(client))
        claim = fleet_claim_wire.acquire_issue_claim(_KEY)
        assert claim is not None
        assert fleet_claim_wire.issue_claim_still_held(_KEY, claim.sha, str(client)) is True

    def test_still_held_unreachable_fails_safe_false(self, tmp_path) -> None:
        # A repo whose origin is unreachable: is_held_by_me raises, the fence
        # catches and treats it as not-held (refuse the outward write).
        client = init_client(tmp_path / "c", tmp_path / "absent.git")
        assert fleet_claim_wire.issue_claim_still_held(_KEY, "deadbeef" * 5, str(client)) is False


class TestFleetClaimEnabledSetting(TestCase):
    def test_default_is_off(self) -> None:
        assert fleet_claim_wire.fleet_claim_enabled("acme") is False
