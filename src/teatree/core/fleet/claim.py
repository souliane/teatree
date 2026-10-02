"""Cross-instance work-claim mutex over GitHub refs (fleet-safety Stage 2).

Stage 1 gave each installation a durable :func:`teatree.instance_id.instance_id`
and a pre-dispatch forge read-back. This module is the actual MUTEX: N teatree
instances (a laptop, a headless box) coordinating on the SAME forge repo can
never double-claim one work item.

The primitive is a **git ref update as a server-side compare-and-swap**. A claim
lives at ``refs/teatree/claims/<slug>`` on the forge remote:

*   ``acquire`` creates the ref with a plain (non-force) push. The receive-pack
    ref transaction rejects the push when the ref already exists and the pushed
    commit is not a fast-forward — and every claim commit carries a random nonce,
    so two claimants never compute the same sha and an existing ref is never a
    fast-forward of a rival's fresh commit. Exactly one create wins.
*   ``heartbeat`` / ``steal_if_expired`` re-point the ref with
    ``git push --force-with-lease=<ref>:<observed-sha>`` — a CAS against the value
    the caller observed. The first CAS lands; every rival CAS carries a now-stale
    expected value and is rejected server-side. Exactly one steal wins.

The commit the ref points at IS the fencing token: its sha is unforgeable and
changes on every steal, so :func:`is_held_by_me` (re-read the ref, compare shas)
is the fence a caller runs before an outward write — a stolen-from instance sees
a sha that is no longer its own and stands down.

No new database: the forge that owns the work domain (issues, branches, PRs) is
the same server that arbitrates the claim, so the mutex is consistent with the
work it guards by construction. The module is deliberately Django-free — it uses
only :mod:`teatree.utils.run` (the sanctioned subprocess boundary),
:func:`teatree.instance_id.instance_id`, and the stdlib — so a claim race can be
exercised by real subprocesses without booting Django.

Zero footprint: every object git writes for a claim operation — the throwaway
claim commit, and the rival commit an expiry probe fetches to read its metadata —
is routed into an EPHEMERAL object directory (:func:`_ephemeral_odb`) that is
deleted when the operation ends. The remote ref is the claim's only durable home,
so nothing local is needed after the push, and the clone teatree pushes from stays
byte-identical. There is therefore nothing to prune, and no pruning pass that could
drop a live claim.
"""

import contextlib
import hashlib
import json
import re
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict, cast

from teatree.instance_id import instance_id
from teatree.utils.git_run import git_env_without_overrides
from teatree.utils.run import CommandFailedError, CompletedProcess, run_allowed_to_fail, run_checked

_REF_PREFIX = "refs/teatree/claims"


class ClaimMeta(TypedDict):
    """The JSON payload carried in a claim commit's message."""

    work_key: str
    instance_id: str
    claimed_at: float
    ttl_seconds: float
    #: Random per-attempt token — guarantees a unique commit sha so two claimants
    #: never compute the same sha (which a plain push would report as success).
    nonce: str


#: A claim not re-affirmed by a ``heartbeat`` within this window is considered
#: abandoned — a surviving instance may ``steal_if_expired`` it. Sized well ABOVE
#: the heartbeat cadence: the in-flight heartbeat sweep runs on the issue-implementer
#: tick (default 1h), so at 4h a live claim is re-affirmed about 4 times per TTL and
#: can never lapse mid-dispatch; a genuinely crashed holder is reclaimable ~4h after
#: its last heartbeat. (``teatree.core.fleet.wire.heartbeat_inflight_claims`` drives the beat.)
DEFAULT_TTL_SECONDS = 14400.0

# The commit that carries the claim metadata is authored under a fixed, repo-
# independent identity so ``commit-tree`` never depends on the local repo's
# ``user.*`` config being set.
_CLAIM_IDENTITY = ("-c", "user.name=teatree-fleet-claim", "-c", "user.email=fleet-claim@teatree.local")

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


class FleetClaimUnavailableError(RuntimeError):
    """The claim ref infrastructure could not be reached.

    Raised when a git command that MUST reach the remote (``ls-remote``,
    ``fetch``) fails for a non-CAS reason — the forge is offline, the remote is
    misconfigured, or auth is missing. A caller wiring the mutex in must treat
    this as *fail-safe*: do NOT claim / do NOT push under an unverifiable claim,
    and log loudly. It is deliberately distinct from a clean contended loss
    (``acquire``/``steal_if_expired`` returning ``None``), which is a normal race
    outcome, not an outage. Named with the ``Error`` suffix per the repo bar.
    """


@dataclass(frozen=True, slots=True)
class Claim:
    """A won claim — the ref name plus the sha that is its fencing token."""

    work_key: str
    ref: str
    #: The commit sha the ref points at. THIS is the fencing token: it is
    #: re-read by :func:`is_held_by_me` and changes on every steal.
    sha: str
    instance_id: str
    claimed_at: float
    ttl_seconds: float

    @classmethod
    def from_token(cls, work_key: str, sha: str) -> "Claim":
        """A fencing-token handle for the fence check, not a live acquired claim.

        A caller (e.g. a ship gate) that persisted only the ref sha rebuilds the
        handle :func:`is_held_by_me` needs from ``work_key`` + ``sha``; the
        liveness fields are irrelevant to the sha comparison and are left empty.
        """
        return cls(work_key=work_key, ref=claim_ref(work_key), sha=sha, instance_id="", claimed_at=0.0, ttl_seconds=0.0)


@dataclass(frozen=True, slots=True)
class ClaimLost:
    """A ``heartbeat`` outcome: the CAS failed because the claim was stolen.

    ``observed_sha`` is what the ref points at now (a rival's fencing token, or
    ``""`` if the ref was deleted), distinct from the ``expected_sha`` the caller
    still held.
    """

    work_key: str
    ref: str
    expected_sha: str
    observed_sha: str


def claim_ref(work_key: str) -> str:
    """The ref path for *work_key* — a readable slug plus a stable hash suffix.

    The hash makes the ref collision-free and always-valid even when two
    different work keys sanitize to the same slug (or a key sanitizes to empty).
    """
    if not work_key:
        msg = "work_key must be non-empty"
        raise ValueError(msg)
    digest = hashlib.sha256(work_key.encode("utf-8")).hexdigest()[:16]
    slug = _SLUG_RE.sub("-", work_key).strip("-")[:60].strip("-")
    return f"{_REF_PREFIX}/{slug}-{digest}" if slug else f"{_REF_PREFIX}/{digest}"


def acquire(
    work_key: str,
    *,
    repo: str = ".",
    remote: str = "origin",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> Claim | None:
    """Create the claim ref for *work_key*, or return ``None`` if already held.

    Returns a :class:`Claim` when this instance created the ref (won the mutex),
    ``None`` when the ref already exists (someone else holds it — a live holder,
    or an expired one to be reclaimed via :func:`steal_if_expired`). Raises
    :class:`FleetClaimUnavailableError` when the remote is unreachable.
    """
    ref = claim_ref(work_key)
    ts = _resolve_now(now)
    inst = instance_id()
    with _ephemeral_odb(repo, remote) as scope:
        sha = _write_claim_commit(scope, _meta(work_key, inst, ts, ttl_seconds))
        created = _try_create(scope, sha, ref)
    if created:
        return Claim(work_key=work_key, ref=ref, sha=sha, instance_id=inst, claimed_at=ts, ttl_seconds=ttl_seconds)
    # The create failed. A present ref means a rival holds it (a normal loss);
    # an absent ref means the push failed for an infra/permission reason.
    if _ls_remote_sha(repo, remote, ref):
        return None
    msg = f"claim push for {ref} failed but the ref is absent (remote unreachable or unwritable)"
    raise FleetClaimUnavailableError(msg)


def steal_if_expired(
    work_key: str,
    *,
    repo: str = ".",
    remote: str = "origin",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> Claim | None:
    """Reclaim an EXPIRED claim via CAS against the expired sha; else ``None``.

    ``None`` when the ref is absent (nothing to steal — use :func:`acquire`), the
    holder is still live, or a concurrent stealer's CAS won the race (this
    instance's CAS carried a now-stale expected value and was rejected
    server-side). Exactly one stealer wins under contention.
    """
    ref = claim_ref(work_key)
    ts = _resolve_now(now)
    snapshot = _fetch_claim(repo, remote, ref)
    if snapshot is None:
        return None
    current_sha, meta = snapshot
    if not _is_expired(meta, ts):
        return None
    inst = instance_id()
    with _ephemeral_odb(repo, remote) as scope:
        new_sha = _write_claim_commit(scope, _meta(work_key, inst, ts, ttl_seconds))
        won = _cas(scope, ref, old_sha=current_sha, new_sha=new_sha)
    if won:
        return Claim(work_key=work_key, ref=ref, sha=new_sha, instance_id=inst, claimed_at=ts, ttl_seconds=ttl_seconds)
    return None


def heartbeat(claim: Claim, *, repo: str = ".", remote: str = "origin", now: float | None = None) -> Claim | ClaimLost:
    """Re-point the ref to a fresh commit via CAS against the caller's OWN sha.

    Returns the refreshed :class:`Claim` when the CAS lands (the ref still held
    this instance's sha). Returns :class:`ClaimLost` only once the ref is READ
    BACK off ``claim.sha`` — mirroring :func:`acquire`'s own failed-push read: a
    push a pre-receive hook or a permission blip rejected leaves the ref on our
    own token, and reporting that as a steal marked a still-owned marker
    ABANDONED. An unchanged ref raises :class:`FleetClaimUnavailableError` so
    the caller leaves the marker live for the next tick. The CAS is against
    ``claim.sha`` (this instance's own token), so a heartbeat can never clobber
    a rival's steal.
    """
    ts = _resolve_now(now)
    with _ephemeral_odb(repo, remote) as scope:
        new_sha = _write_claim_commit(scope, _meta(claim.work_key, claim.instance_id, ts, claim.ttl_seconds))
        landed = _cas(scope, claim.ref, old_sha=claim.sha, new_sha=new_sha)
    if landed:
        return replace(claim, sha=new_sha, claimed_at=ts)
    observed = _ls_remote_sha(repo, remote, claim.ref)
    if observed == claim.sha:
        msg = f"heartbeat push for {claim.ref} failed but the ref still holds our sha (remote unwritable)"
        raise FleetClaimUnavailableError(msg)
    return ClaimLost(
        work_key=claim.work_key,
        ref=claim.ref,
        expected_sha=claim.sha,
        observed_sha=observed,
    )


def release(claim: Claim, *, repo: str = ".", remote: str = "origin") -> None:
    """Best-effort delete of the claim ref, CAS-guarded against ``claim.sha``.

    The delete is a ``--force-with-lease`` against this instance's own sha, so a
    stale release (the claim was already stolen) is a no-op that never removes a
    rival's live claim. Every failure is swallowed — releasing is advisory
    cleanup; the TTL is the real backstop.
    """
    with contextlib.suppress(Exception):
        _cas_delete(repo, remote, claim.ref, old_sha=claim.sha)


def is_held_by_me(work_key: str, claim: Claim, *, repo: str = ".", remote: str = "origin") -> bool:
    """THE fence: re-read the ref and return whether it still points at ``claim.sha``.

    ``False`` when the ref was stolen (its sha changed) or deleted. Raises
    :class:`FleetClaimUnavailableError` when the remote is unreachable — a caller
    must treat that as "cannot confirm ownership" and refuse the outward write,
    same fail-safe posture as a lost fence.
    """
    if not claim.sha:
        return False
    return _ls_remote_sha(repo, remote, claim_ref(work_key)) == claim.sha


def _resolve_now(now: float | None) -> float:
    # Injectable wall clock: production passes nothing (real time); tests pass a
    # fixed epoch so TTL expiry is deterministic without sleeping. A cross-machine
    # claim needs an absolute wall clock, not a monotonic one.
    return time.time() if now is None else now


def _meta(work_key: str, inst: str, claimed_at: float, ttl_seconds: float) -> ClaimMeta:
    return {
        "work_key": work_key,
        "instance_id": inst,
        "claimed_at": claimed_at,
        "ttl_seconds": ttl_seconds,
        "nonce": uuid.uuid4().hex,
    }


def _git(repo: str, args: tuple[str, ...] | list[str], *, env: dict[str, str] | None = None) -> CompletedProcess[str]:
    # Every remote/read op tolerates a non-zero exit (the caller inspects the
    # returncode); the local claim-commit writes use run_checked directly.
    return run_allowed_to_fail(["git", "-C", repo, *args], expected_codes=None, env=env or git_env_without_overrides())


def _objects_dir(repo: str) -> str:
    result = _git(repo, ["rev-parse", "--git-path", "objects"])
    if result.returncode != 0:
        msg = f"cannot resolve the object directory of {repo} (not a git repo?)"
        raise FleetClaimUnavailableError(msg)
    # ``-C repo`` makes git's relative answer relative to *repo*; an absolute
    # answer (worktree, GIT_DIR-style layout) wins the join on its own.
    return str(Path(repo) / result.stdout.strip())


@dataclass(frozen=True, slots=True)
class _Scope:
    """One claim operation's clone, remote, and ephemeral-object-dir git env."""

    repo: str
    remote: str
    env: dict[str, str]


@contextlib.contextmanager
def _ephemeral_odb(repo: str, remote: str) -> Iterator[_Scope]:
    """A scope routing every object git WRITES during one claim op into a throwaway dir.

    The clone's real object DB is attached as a read-only alternate — push and
    fetch negotiation walk local refs, which resolve only through it — so the
    scope reads everything the clone has and contributes nothing back to it.
    """
    env = git_env_without_overrides()
    alternate = _objects_dir(repo)
    with tempfile.TemporaryDirectory(prefix="t3-claim-odb-") as scratch:
        overrides = {"GIT_OBJECT_DIRECTORY": scratch, "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate}
        yield _Scope(repo=repo, remote=remote, env=env | overrides)


def _empty_tree(scope: _Scope) -> str:
    return run_checked(["git", "-C", scope.repo, "mktree"], stdin_text="", env=scope.env).stdout.strip()


def _write_claim_commit(scope: _Scope, meta: ClaimMeta) -> str:
    # mktree + commit-tree, written into the scope's ephemeral object dir. Per the
    # module contract any inability to build a claim commit surfaces as
    # FleetClaimUnavailableError (never a bare CommandFailedError), so the wire's
    # fail-safe catch handles a corrupt local repo uniformly with remote-unreachable.
    message = json.dumps(meta, sort_keys=True)
    try:
        args = ["git", "-C", scope.repo, *_CLAIM_IDENTITY, "commit-tree", _empty_tree(scope), "-m", message]
        return run_checked(args, env=scope.env).stdout.strip()
    except CommandFailedError as exc:
        msg = f"cannot write the claim commit in {scope.repo} (local git failure)"
        raise FleetClaimUnavailableError(msg) from exc


def _try_create(scope: _Scope, sha: str, ref: str) -> bool:
    # Assumes each claim commit is unique (the nonce in _meta): so an existing ref
    # is never a fast-forward of this fresh commit, and a plain push succeeds ONLY
    # as a create — an idempotent "already there" success (two claimants, one sha)
    # cannot occur.
    return _git(scope.repo, ["push", scope.remote, f"{sha}:{ref}"], env=scope.env).returncode == 0


def _cas(scope: _Scope, ref: str, *, old_sha: str, new_sha: str) -> bool:
    args = ["push", f"--force-with-lease={ref}:{old_sha}", scope.remote, f"{new_sha}:{ref}"]
    return _git(scope.repo, args, env=scope.env).returncode == 0


def _cas_delete(repo: str, remote: str, ref: str, *, old_sha: str) -> bool:
    args = ["push", f"--force-with-lease={ref}:{old_sha}", remote, f":{ref}"]
    return _git(repo, args).returncode == 0


def _ls_remote_sha(repo: str, remote: str, ref: str) -> str:
    result = _git(repo, ["ls-remote", remote, ref])
    if result.returncode != 0:
        msg = f"ls-remote {ref} failed (remote unreachable): {result.stderr.strip()}"
        raise FleetClaimUnavailableError(msg)
    line = result.stdout.strip()
    return line.split()[0] if line else ""


def _fetch_claim(repo: str, remote: str, ref: str) -> tuple[str, ClaimMeta | None] | None:
    """Return the ref's current ``(sha, metadata)`` or ``None`` when absent.

    The commit is fetched into an ephemeral object dir with no destination
    refspec and no ``FETCH_HEAD``, so reading a rival's metadata mutates neither
    the clone's object DB nor its ref store. The sha is the one ``ls-remote``
    reported, so a steal CASes against exactly the value whose metadata drove the
    expiry decision; a ref that moved under us leaves that sha unfetched, the read
    fails, and the steal is skipped this round (fail-safe).
    """
    tip = _ls_remote_sha(repo, remote, ref)
    if not tip:
        return None
    with _ephemeral_odb(repo, remote) as scope:
        fetch = ["-c", "gc.auto=0", "fetch", "--quiet", "--no-write-fetch-head", remote, ref]
        if _git(repo, fetch, env=scope.env).returncode != 0:
            msg = f"fetch {ref} failed (remote unreachable)"
            raise FleetClaimUnavailableError(msg)
        read = _git(repo, ["log", "-1", "--format=%B", tip], env=scope.env)
    return (tip, _parse_meta(read.stdout)) if read.returncode == 0 else None


def _parse_meta(body: str) -> ClaimMeta | None:
    body = body.strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return cast("ClaimMeta", data) if isinstance(data, dict) else None


def _is_expired(meta: ClaimMeta | None, now: float) -> bool:
    if meta is None:
        return False  # unreadable metadata is never treated as expired — do not steal a claim we cannot read
    try:
        claimed_at = float(meta["claimed_at"])
        ttl = float(meta["ttl_seconds"])
    except (KeyError, TypeError, ValueError):
        return False
    return now >= claimed_at + ttl
