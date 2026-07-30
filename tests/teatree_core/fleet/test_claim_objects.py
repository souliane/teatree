"""The claim mutex leaves ZERO objects behind in the clone it pushes from.

A claim commit is a fencing token whose only durable home is the remote ref, so
nothing about the mutex needs it in the local object DB. The bug class guarded
here: an ``acquire``/``heartbeat``/``steal`` that writes into the shared clone
leaves an unreachable loose object, nothing on the claim path ever triggers git's
auto-gc, and the footprint then grows without bound (one object per in-flight
claim per heartbeat tick) for as long as ``fleet_claim_enabled`` is on.

Anti-vacuity: :func:`test_probe_detects_an_unisolated_object_write` plants that
leaking write shape (the module's own writer, minus the ephemeral object dir) and
asserts the same probe goes RED, so a green footprint assertion above is carried
by the isolation, not by a blind probe. The fail-CLOSED half is
:class:`TestLiveClaimSurvivesTheZeroFootprintCycle`: a live claim must still be
enforceable — and its metadata still READABLE from the remote — after the cycle,
so "leave nothing behind" can never be satisfied by dropping a live claim.
"""

from pathlib import Path

from teatree.core.fleet import claim as fleet_claim
from teatree.utils.git_run import git_env_without_overrides

from ._git_origin import git, init_bare, init_client, ref_sha

_WORK_KEY = "https://github.com/souliane/teatree/issues/4242"
_TTL = 100.0


def _loose_objects(repo: Path) -> set[Path]:
    root = repo / ".git" / "objects"
    return {path for path in root.rglob("*") if path.is_file() and "pack" not in path.relative_to(root).parts}


def _origin(tmp_path: Path, name: str = "clone") -> tuple[Path, Path]:
    bare = init_bare(tmp_path / "origin.git")
    return bare, init_client(tmp_path / name, bare)


def _write_claim_commit_unisolated(repo: Path) -> None:
    """The leaking write shape — the module's own writer, minus the ephemeral object dir."""
    scope = fleet_claim._Scope(repo=str(repo), remote="origin", env=git_env_without_overrides())
    fleet_claim._write_claim_commit(scope, fleet_claim._meta(_WORK_KEY, "instance", 0.0, _TTL))


def test_probe_detects_an_unisolated_object_write(tmp_path: Path) -> None:
    _, clone = _origin(tmp_path)
    before = _loose_objects(clone)
    _write_claim_commit_unisolated(clone)
    assert _loose_objects(clone) > before


def test_acquire_and_heartbeat_write_no_objects_into_the_clone(tmp_path: Path) -> None:
    _, clone = _origin(tmp_path)
    before = _loose_objects(clone)
    held = fleet_claim.acquire(_WORK_KEY, repo=str(clone), ttl_seconds=_TTL, now=0.0)
    assert isinstance(held, fleet_claim.Claim)
    for beat in range(1, 4):
        held = fleet_claim.heartbeat(held, repo=str(clone), now=float(beat))
        assert isinstance(held, fleet_claim.Claim)
    assert _loose_objects(clone) == before


def test_release_writes_no_objects_into_the_clone(tmp_path: Path) -> None:
    _, clone = _origin(tmp_path)
    held = fleet_claim.acquire(_WORK_KEY, repo=str(clone), ttl_seconds=_TTL, now=0.0)
    assert isinstance(held, fleet_claim.Claim)
    before = _loose_objects(clone)
    fleet_claim.release(held, repo=str(clone))
    assert _loose_objects(clone) == before


def test_contended_acquire_and_expiry_probe_write_no_objects_into_the_rival_clone(tmp_path: Path) -> None:
    bare, holder = _origin(tmp_path, "holder")
    rival = init_client(tmp_path / "rival", bare)
    assert fleet_claim.acquire(_WORK_KEY, repo=str(holder), ttl_seconds=_TTL, now=0.0) is not None
    before = _loose_objects(rival)
    # A lost acquire still built (and pushed nothing of) its own claim commit, and
    # the expiry probe fetches the HOLDER's commit to read its metadata.
    assert fleet_claim.acquire(_WORK_KEY, repo=str(rival), ttl_seconds=_TTL, now=1.0) is None
    assert fleet_claim.steal_if_expired(_WORK_KEY, repo=str(rival), ttl_seconds=_TTL, now=1.0) is None
    assert _loose_objects(rival) == before


def test_steal_writes_no_objects_into_the_stealing_clone(tmp_path: Path) -> None:
    bare, holder = _origin(tmp_path, "holder")
    rival = init_client(tmp_path / "rival", bare)
    assert fleet_claim.acquire(_WORK_KEY, repo=str(holder), ttl_seconds=_TTL, now=0.0) is not None
    before = _loose_objects(rival)
    stolen = fleet_claim.steal_if_expired(_WORK_KEY, repo=str(rival), ttl_seconds=_TTL, now=_TTL + 1.0)
    assert isinstance(stolen, fleet_claim.Claim)
    assert ref_sha(bare, stolen.ref) == stolen.sha
    assert _loose_objects(rival) == before


def test_the_expiry_probe_leaves_no_ref_behind_in_the_clone(tmp_path: Path) -> None:
    bare, holder = _origin(tmp_path, "holder")
    rival = init_client(tmp_path / "rival", bare)
    assert fleet_claim.acquire(_WORK_KEY, repo=str(holder), ttl_seconds=_TTL, now=0.0) is not None
    fleet_claim.steal_if_expired(_WORK_KEY, repo=str(rival), ttl_seconds=_TTL, now=1.0)
    assert git(rival, "for-each-ref", "--format=%(refname)") == ""


class TestLiveClaimSurvivesTheZeroFootprintCycle:
    """Fail-CLOSED: leaving nothing behind must never cost a LIVE claim."""

    def _held_after_cycle(self, tmp_path: Path) -> tuple[Path, Path, fleet_claim.Claim]:
        bare, holder = _origin(tmp_path, "holder")
        held = fleet_claim.acquire(_WORK_KEY, repo=str(holder), ttl_seconds=_TTL, now=0.0)
        assert isinstance(held, fleet_claim.Claim)
        for beat in range(1, 4):
            beaten = fleet_claim.heartbeat(held, repo=str(holder), now=float(beat))
            assert isinstance(beaten, fleet_claim.Claim)
            held = beaten
        return bare, holder, held

    def test_the_ref_still_carries_the_holders_fencing_token(self, tmp_path: Path) -> None:
        bare, holder, held = self._held_after_cycle(tmp_path)
        assert ref_sha(bare, held.ref) == held.sha
        assert fleet_claim.is_held_by_me(_WORK_KEY, held, repo=str(holder)) is True

    def test_a_rival_still_loses_the_acquire(self, tmp_path: Path) -> None:
        bare, _holder, held = self._held_after_cycle(tmp_path)
        rival = init_client(tmp_path / "rival", bare)
        assert fleet_claim.acquire(_WORK_KEY, repo=str(rival), ttl_seconds=_TTL, now=4.0) is None
        assert ref_sha(bare, held.ref) == held.sha

    def test_the_metadata_stays_readable_so_expiry_is_still_decided_on_it(self, tmp_path: Path) -> None:
        # The sharp pair: unreadable metadata would ALSO refuse the in-TTL steal
        # (``_is_expired(None)`` is False), so the past-TTL steal is what proves the
        # live claim commit — not just its ref — survived the cycle intact.
        bare, _holder, held = self._held_after_cycle(tmp_path)
        rival = init_client(tmp_path / "rival", bare)
        assert fleet_claim.steal_if_expired(_WORK_KEY, repo=str(rival), ttl_seconds=_TTL, now=4.0) is None
        stolen = fleet_claim.steal_if_expired(_WORK_KEY, repo=str(rival), ttl_seconds=_TTL, now=held.claimed_at + _TTL)
        assert isinstance(stolen, fleet_claim.Claim)
