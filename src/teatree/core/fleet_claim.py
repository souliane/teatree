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
"""

import contextlib
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import TypedDict, cast

from teatree.instance_id import instance_id
from teatree.utils.git_run import git_env_without_overrides
from teatree.utils.run import CompletedProcess, run_allowed_to_fail, run_checked

_REF_PREFIX = "refs/teatree/claims"
_PROBE_PREFIX = "refs/teatree/_probe"


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
#: abandoned — a surviving instance may ``steal_if_expired`` it. Long enough that
#: a live holder's per-tick heartbeat never lapses; short enough that a crashed
#: holder does not wedge the work item for long.
DEFAULT_TTL_SECONDS = 3600.0

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
    sha = _write_claim_commit(repo, _meta(work_key, inst, ts, ttl_seconds))
    if _try_create(repo, remote, sha, ref):
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
    new_sha = _write_claim_commit(repo, _meta(work_key, inst, ts, ttl_seconds))
    if _cas(repo, remote, ref, old_sha=current_sha, new_sha=new_sha):
        return Claim(work_key=work_key, ref=ref, sha=new_sha, instance_id=inst, claimed_at=ts, ttl_seconds=ttl_seconds)
    return None


def heartbeat(claim: Claim, *, repo: str = ".", remote: str = "origin", now: float | None = None) -> Claim | ClaimLost:
    """Re-point the ref to a fresh commit via CAS against the caller's OWN sha.

    Returns the refreshed :class:`Claim` when the CAS lands (the ref still held
    this instance's sha). Returns :class:`ClaimLost` when it does not — the ref
    no longer points at ``claim.sha``, so another instance stole it. The CAS is
    against ``claim.sha`` (this instance's own token), so a heartbeat can never
    clobber a rival's steal.
    """
    ts = _resolve_now(now)
    new_sha = _write_claim_commit(repo, _meta(claim.work_key, claim.instance_id, ts, claim.ttl_seconds))
    if _cas(repo, remote, claim.ref, old_sha=claim.sha, new_sha=new_sha):
        return replace(claim, sha=new_sha, claimed_at=ts)
    return ClaimLost(
        work_key=claim.work_key,
        ref=claim.ref,
        expected_sha=claim.sha,
        observed_sha=_ls_remote_sha(repo, remote, claim.ref),
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


def _git(repo: str, args: tuple[str, ...] | list[str], *, allow_fail: bool = False) -> CompletedProcess[str]:
    cmd = ["git", "-C", repo, *args]
    env = git_env_without_overrides()
    if allow_fail:
        return run_allowed_to_fail(cmd, expected_codes=None, env=env)
    return run_checked(cmd, env=env)


def _empty_tree(repo: str) -> str:
    return run_checked(["git", "-C", repo, "mktree"], stdin_text="", env=git_env_without_overrides()).stdout.strip()


def _write_claim_commit(repo: str, meta: ClaimMeta) -> str:
    message = json.dumps(meta, sort_keys=True)
    args = (*_CLAIM_IDENTITY, "commit-tree", _empty_tree(repo), "-m", message)
    return _git(repo, args).stdout.strip()


def _try_create(repo: str, remote: str, sha: str, ref: str) -> bool:
    return _git(repo, ["push", remote, f"{sha}:{ref}"], allow_fail=True).returncode == 0


def _cas(repo: str, remote: str, ref: str, *, old_sha: str, new_sha: str) -> bool:
    args = ["push", f"--force-with-lease={ref}:{old_sha}", remote, f"{new_sha}:{ref}"]
    return _git(repo, args, allow_fail=True).returncode == 0


def _cas_delete(repo: str, remote: str, ref: str, *, old_sha: str) -> bool:
    args = ["push", f"--force-with-lease={ref}:{old_sha}", remote, f":{ref}"]
    return _git(repo, args, allow_fail=True).returncode == 0


def _ls_remote_sha(repo: str, remote: str, ref: str) -> str:
    result = _git(repo, ["ls-remote", remote, ref], allow_fail=True)
    if result.returncode != 0:
        msg = f"ls-remote {ref} failed (remote unreachable): {result.stderr.strip()}"
        raise FleetClaimUnavailableError(msg)
    line = result.stdout.strip()
    return line.split()[0] if line else ""


def _fetch_claim(repo: str, remote: str, ref: str) -> tuple[str, ClaimMeta | None] | None:
    """Return the ref's current ``(sha, metadata)`` or ``None`` when absent.

    Fetches the claim commit locally so its message (the metadata) is readable;
    the returned sha is the just-fetched tip, so a steal CASes against exactly
    the value whose metadata drove the expiry decision.
    """
    if not _ls_remote_sha(repo, remote, ref):
        return None
    probe = f"{_PROBE_PREFIX}/{uuid.uuid4().hex}"
    if _git(repo, ["fetch", "--quiet", remote, f"+{ref}:{probe}"], allow_fail=True).returncode != 0:
        msg = f"fetch {ref} failed (remote unreachable)"
        raise FleetClaimUnavailableError(msg)
    try:
        tip = _git(repo, ["rev-parse", probe], allow_fail=True).stdout.strip()
        body = _git(repo, ["log", "-1", "--format=%B", probe], allow_fail=True).stdout
    finally:
        _git(repo, ["update-ref", "-d", probe], allow_fail=True)
    return tip, _parse_meta(body)


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
