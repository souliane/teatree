"""Real-process CAS proof for the cross-instance claim mutex (fleet-safety Stage 2).

N real OS processes — each its own git clone, modelling a distinct fleet
instance — race :mod:`teatree.core.fleet.claim` against a **local bare git repo
used as origin**. A ``file://`` push exercises the SAME receive-pack ref
transaction (and therefore the same server-side compare-and-swap) as a GitHub
push, so no network is needed and the guarantee under test is the production
guarantee.

Anti-vacuity (how these tests guard the MECHANISM, not themselves): the acquire
race asserts *exactly one* winner across N processes. The current
``ImplementedIssueMarker.claim()`` cannot provide that cross-process — its
``get_or_create`` runs against **per-instance** SQLite, so each instance's DB is
empty and every instance's insert succeeds. ``test_local_only_claim_...`` is that
counter-proof run through the SAME race harness with per-process isolated SQLite
(the shape ``claim()`` has on ``main``): it asserts **N** winners — a
double-claim. The ONLY difference between the two races is the shared claim ref,
so the exactly-one assertion is carried by the ref CAS, not by the harness.
Concretely: drop the push CAS from ``acquire`` (make it a local-only create) and
the acquire race's winner count jumps from 1 to N — the same red the local-only
counter-proof already pins.

Backend under test: a real ``git init --bare`` origin on ``tmp_path``, real
``git`` subprocesses, real ``multiprocessing`` (fork context, so the guarantee
holds identically on the Linux/CI fork default and a local macOS run).
"""

import json
import multiprocessing
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from teatree.core.fleet import claim as fleet_claim

_CTX = multiprocessing.get_context("fork")
_WORK_KEY = "https://github.com/souliane/teatree/issues/4242"
_GATE_TIMEOUT_S = 30.0


def _git(*args: str) -> str:
    cmd = ["git", *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def _init_bare(path: Path) -> None:
    _git("init", "--bare", "-q", str(path))


def _init_client(client_dir: Path, bare: Path) -> None:
    """A fresh clone-shaped repo whose ``origin`` is the shared bare repo.

    No ``user.*`` config is set — ``fleet_claim`` authors its claim commits under
    a fixed identity, so a client needs only the ``origin`` remote.
    """
    _git("init", "-b", "main", "-q", str(client_dir))
    _git("-C", str(client_dir), "remote", "add", "origin", f"file://{bare}")


def _wait_for(path: Path, timeout: float = _GATE_TIMEOUT_S) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        if time.time() > deadline:
            pytest.fail(f"gate file never appeared: {path}")
        time.sleep(0.005)


def _gate(work_dir: str) -> Path:
    return Path(work_dir) / "gate"


def _results(work_dir: str) -> Path:
    return Path(work_dir) / "results"


def _run_race(n: int, worker: object, work_dir: Path, extra: tuple[object, ...] = ()) -> list[dict]:
    """Release *n* worker processes into the critical section simultaneously.

    Each worker builds its client, drops a ``ready_<i>`` file, then blocks on the
    shared ``go`` file. The parent creates ``go`` only once all *n* are ready, so
    the contended ``git push`` is a real race, not serialized by process-start
    latency. Each worker writes its outcome to ``result_<i>.json``.
    """
    gate = _gate(str(work_dir))
    results = _results(str(work_dir))
    gate.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)

    procs = [_CTX.Process(target=worker, args=(i, str(work_dir), *extra)) for i in range(n)]
    for proc in procs:
        proc.start()
    for i in range(n):
        _wait_for(gate / f"ready_{i}")
    (gate / "go").write_text("go", encoding="utf-8")
    for proc in procs:
        proc.join(timeout=_GATE_TIMEOUT_S)

    return [json.loads((results / f"result_{i}.json").read_text(encoding="utf-8")) for i in range(n)]


def _ready_and_wait(work_dir: str, idx: int) -> None:
    (_gate(work_dir) / f"ready_{idx}").write_text("1", encoding="utf-8")
    _wait_for(_gate(work_dir) / "go")


def _record(work_dir: str, idx: int, outcome: dict[str, object]) -> None:
    (_results(work_dir) / f"result_{idx}.json").write_text(json.dumps(outcome), encoding="utf-8")


# --- workers (top-level so the fork children resolve them) --------------------


def _acquire_worker(idx: int, work_dir: str, bare: str, clients_root: str) -> None:
    client = Path(clients_root) / f"client_{idx}"
    _init_client(client, Path(bare))
    _ready_and_wait(work_dir, idx)
    outcome: dict[str, object] = {"won": False, "sha": "", "error": ""}
    try:
        claim = fleet_claim.acquire(_WORK_KEY, repo=str(client), remote="origin", ttl_seconds=3600.0, now=1000.0)
        outcome = {"won": claim is not None, "sha": claim.sha if claim else "", "error": ""}
    except Exception as exc:  # noqa: BLE001 — a worker records its failure for the parent to assert on
        outcome["error"] = repr(exc)
    _record(work_dir, idx, outcome)


def _steal_worker(idx: int, work_dir: str, bare: str, clients_root: str, now: float) -> None:
    client = Path(clients_root) / f"stealer_{idx}"
    _init_client(client, Path(bare))
    _ready_and_wait(work_dir, idx)
    outcome: dict[str, object] = {"won": False, "sha": "", "error": ""}
    try:
        claim = fleet_claim.steal_if_expired(_WORK_KEY, repo=str(client), remote="origin", ttl_seconds=100.0, now=now)
        outcome = {"won": claim is not None, "sha": claim.sha if claim else "", "error": ""}
    except Exception as exc:  # noqa: BLE001 — a worker records its failure for the parent to assert on
        outcome["error"] = repr(exc)
    _record(work_dir, idx, outcome)


def _local_only_worker(idx: int, work_dir: str, dbs_root: str) -> None:
    """The pre-Stage-2 shape: a per-instance-SQLite ``get_or_create`` claim.

    Each worker owns an ISOLATED SQLite file (one per fleet instance), so the
    UNIQUE(work_key) insert always succeeds — the cross-instance double-claim the
    ref mutex exists to prevent.
    """
    db = Path(dbs_root) / f"instance_{idx}.sqlite3"
    _ready_and_wait(work_dir, idx)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS claims (work_key TEXT PRIMARY KEY)")
        try:
            conn.execute("INSERT INTO claims (work_key) VALUES (?)", (_WORK_KEY,))
            conn.commit()
            won = True
        except sqlite3.IntegrityError:
            won = False
    finally:
        conn.close()
    _record(work_dir, idx, {"won": won})


# --- tests --------------------------------------------------------------------


@pytest.mark.parametrize("n", [2, 10])
def test_acquire_has_exactly_one_winner(tmp_path: Path, n: int) -> None:
    """N processes race ``acquire``; exactly one creates the ref, the rest lose."""
    bare = tmp_path / "origin.git"
    _init_bare(bare)
    clients = tmp_path / "clients"
    clients.mkdir()

    results = _run_race(n, _acquire_worker, tmp_path, extra=(str(bare), str(clients)))

    assert all(r["error"] == "" for r in results), [r["error"] for r in results if r["error"]]
    winners = [r for r in results if r["won"]]
    assert len(winners) == 1, f"expected exactly one winner among {n}, got {len(winners)}: {results}"
    # The winner's fencing token matches the sha actually on the ref.
    ref = fleet_claim.claim_ref(_WORK_KEY)
    assert _git("ls-remote", str(bare), ref).split()[0] == winners[0]["sha"]


@pytest.mark.parametrize("n", [2, 10])
def test_local_only_claim_double_claims_across_instances(tmp_path: Path, n: int) -> None:
    """Anti-vacuity counter-proof: per-instance SQLite lets EVERY instance win.

    Same race harness as the acquire test, but through the pre-Stage-2 local
    ``get_or_create`` shape (isolated SQLite per instance). All N win — the
    exactly-once invariant does NOT hold cross-process without the shared ref, so
    the acquire test's ``== 1`` is guarded by the ref CAS, not by the harness.
    """
    dbs = tmp_path / "dbs"
    dbs.mkdir()

    results = _run_race(n, _local_only_worker, tmp_path, extra=(str(dbs),))

    winners = [r for r in results if r["won"]]
    assert len(winners) == n, f"per-instance SQLite must double-claim: expected {n} winners, got {len(winners)}"


def test_steal_impossible_before_ttl(tmp_path: Path) -> None:
    """A live (unexpired) claim cannot be stolen — every stealer stands down."""
    bare = tmp_path / "origin.git"
    _init_bare(bare)
    clients = tmp_path / "clients"
    clients.mkdir()
    holder_repo = clients / "holder"
    _init_client(holder_repo, bare)
    # Holder claims at t=1000 with ttl=100 -> live until t=1100.
    held = fleet_claim.acquire(_WORK_KEY, repo=str(holder_repo), remote="origin", ttl_seconds=100.0, now=1000.0)
    assert held is not None

    # Five stealers race at t=1050 (still live).
    results = _run_race(5, _steal_worker, tmp_path, extra=(str(bare), str(clients), 1050.0))

    assert all(r["error"] == "" for r in results), [r["error"] for r in results if r["error"]]
    assert [r for r in results if r["won"]] == [], f"a live claim was stolen: {results}"
    # The holder still holds it — no failed steal disturbed the live claim.
    assert fleet_claim.is_held_by_me(_WORK_KEY, held, repo=str(holder_repo), remote="origin")


@pytest.mark.parametrize("n", [2, 10])
def test_concurrent_steal_after_ttl_has_exactly_one_winner(tmp_path: Path, n: int) -> None:
    """After TTL, N stealers race; exactly one CAS lands, the rest are fenced out."""
    bare = tmp_path / "origin.git"
    _init_bare(bare)
    clients = tmp_path / "clients"
    clients.mkdir()
    holder_repo = clients / "holder"
    _init_client(holder_repo, bare)
    # Holder claims at t=1000 with ttl=100 -> expired by t=5000.
    held = fleet_claim.acquire(_WORK_KEY, repo=str(holder_repo), remote="origin", ttl_seconds=100.0, now=1000.0)
    assert held is not None

    results = _run_race(n, _steal_worker, tmp_path, extra=(str(bare), str(clients), 5000.0))

    assert all(r["error"] == "" for r in results), [r["error"] for r in results if r["error"]]
    winners = [r for r in results if r["won"]]
    assert len(winners) == 1, f"expected exactly one steal winner among {n}, got {len(winners)}: {results}"
    # Fencing: the original holder is now stolen-from and must read False.
    assert not fleet_claim.is_held_by_me(_WORK_KEY, held, repo=str(holder_repo), remote="origin")


def test_chaos_dead_holder_is_stolen_after_ttl_and_revived_original_is_fenced(tmp_path: Path) -> None:
    """A dead holder is stolen after TTL; the revived original is fenced.

    The holder acquires then dies (never heartbeats); a survivor steals once the
    TTL lapses; the revived original reads fenced at ``is_held_by_me`` and
    ``heartbeat``.
    """
    bare = tmp_path / "origin.git"
    _init_bare(bare)
    dead = tmp_path / "dead"
    survivor = tmp_path / "survivor"
    _init_client(dead, bare)
    _init_client(survivor, bare)

    original = fleet_claim.acquire(_WORK_KEY, repo=str(dead), remote="origin", ttl_seconds=10.0, now=1000.0)
    assert original is not None
    # The holder dies: it never heartbeats. While it is alive (before TTL) no one
    # can steal.
    assert fleet_claim.steal_if_expired(_WORK_KEY, repo=str(survivor), remote="origin", now=1005.0) is None
    assert fleet_claim.is_held_by_me(_WORK_KEY, original, repo=str(dead), remote="origin")

    # After the TTL lapses the survivor steals.
    stolen = fleet_claim.steal_if_expired(_WORK_KEY, repo=str(survivor), remote="origin", ttl_seconds=10.0, now=2000.0)
    assert stolen is not None
    assert stolen.sha != original.sha

    # The revived original is fenced: it no longer holds the ref, and its own
    # heartbeat CAS (against its stale sha) reports the claim lost.
    assert not fleet_claim.is_held_by_me(_WORK_KEY, original, repo=str(dead), remote="origin")
    lost = fleet_claim.heartbeat(original, repo=str(dead), remote="origin", now=2001.0)
    assert isinstance(lost, fleet_claim.ClaimLost)
    assert lost.expected_sha == original.sha
    assert lost.observed_sha == stolen.sha
    # The survivor can still heartbeat its live claim.
    beat = fleet_claim.heartbeat(stolen, repo=str(survivor), remote="origin", now=2002.0)
    assert isinstance(beat, fleet_claim.Claim)
    assert fleet_claim.is_held_by_me(_WORK_KEY, beat, repo=str(survivor), remote="origin")
