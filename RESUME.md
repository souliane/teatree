# RESUME — scanner/worker DB connection hygiene (RED main)

Branch: `fix/scanner-pool-conn-hygiene`, based on `origin/main` @ `9ce01693`.
Work commit: `42732ccc` — "fix(db): close every worker thread's raw DB handle, not just the scanner pool".
Working tree is CLEAN. Nothing is stranded.

## The single most important finding — the handover premise was FALSE

I was told to rescue and land commit `8a35ea35` because it was "stranded on a
detached HEAD with no refs". **It is not stranded. It is already on `origin/main`.**

Verified two ways:

```
git diff 8a35ea35:src/teatree/loop/phases/scan.py origin/main:src/teatree/loop/phases/scan.py   # empty
git diff 8a35ea35:tests/teatree_loop/phases/test_scan.py origin/main:tests/teatree_loop/phases/test_scan.py  # empty
git cherry-pick 8a35ea35   # -> "nothing to commit, working tree clean"
```

`8a35ea35` was authored 09:36 +0200 on the `#3520` branch and rode into `main`
inside the `#3520` squash (`9ce01693`, 10:12 +0200). So:

- **`9ce01693` — the commit that went RED — ALREADY CONTAINS the fix.**
- PR #3529's branch also contains it (`9ce01693` is an ancestor) and still went red.

Conclusion: `8a35ea35` was **correct but incomplete**. Landing it again was a no-op.
Do not re-open a PR that just re-lands it.

## Root cause (confirmed, with a local reproduction)

`pyproject.toml:326-331` sets `filterwarnings = ["error", ...]`. CPython emits
`ResourceWarning: unclosed database` when it finalizes a stranded `sqlite3.Connection`.
Under `-n auto` the GC fires at an arbitrary moment, so the error is attributed to
whatever unrelated test was running in that xdist worker. **The reported test is always
a victim, never the culprit** — hence `test_run.py` on shard 9 and
`test_retro_gate_failures.py` on shard 2.

CI log (job 88585174399) confirms the warning class:

```
E   ResourceWarning: unclosed database in <sqlite3.Connection object at 0x7ffa56362b60>
E   pytest.PytestUnraisableExceptionWarning: Exception ignored in: <sqlite3.Connection object ...>
```

The culprit is any thread that touches the ORM and exits without closing its
thread-local Django connection.

### The subtlety that made `8a35ea35` incomplete

`connections.close_all()` does **not** release the handle under the test DB. Django's
sqlite backend makes `close()` a deliberate no-op for an in-memory database:

`.venv/lib/python3.13/site-packages/django/db/backends/sqlite3/base.py:221-227`

```python
def close(self):
    self.validate_thread_sharing()
    # If database is in memory, closing the connection destroys the
    # database. To prevent accidental data loss, ignore close requests on
    # an in-memory db.
    if not self.is_in_memory_db():
        BaseDatabaseWrapper.close(self)
```

**The handover called `src/teatree/loops/worker.py:135-148` the "precedent" to copy.
It is not a precedent — it was itself a leak site.** It called
`connections.close_all()`, i.e. exactly the ineffective form. `8a35ea35` fixed the
scanner pool inline and left four other worker sites leaking.

## What I changed

New shared primitive `src/teatree/utils/thread_db.py`:
`close_thread_db_connections()` closes the **raw DB-API handle** and dereferences the
wrapper. It **refuses to run on the main thread**, so it can never close the connection
a `TestCase` wraps its transaction in (that would tear the shared in-memory DB out
from under the suite).

Wired into every worker site:

| Site | Was |
|---|---|
| `src/teatree/loop/phases/scan.py` | inline duplicate from `8a35ea35` — replaced by the shared helper |
| `src/teatree/loops/worker.py` | `connections.close_all()` — **ineffective**, real leak |
| `src/teatree/core/worktree/readiness.py` (`run_probes`) | **no hygiene at all** |
| `.../_workspace/provision_parallel.py` | **no hygiene at all** |
| `src/teatree/core/provision/provision_timebox.py` (callable worker thread) | **no hygiene at all** |

`src/teatree/core/provision/step_runner.py:300-322` is already covered — it hoists the
ORM read onto the caller thread and documents this exact ResourceWarning. Left alone.

Regression guards added per site (all mirror `TestScanPhaseConnectionHygiene`):
`tests/teatree_utils/test_thread_db.py` (new), plus `TestRunProbesConnectionHygiene`,
`TestProvisionPoolConnectionHygiene`, `TestExecutorThreadConnectionHygiene`.

## Anti-vacuity result — PASSED, and it reproduced the production symptom

I did NOT revert `scan.py` alone; I did something stronger — neutered the shared helper
with an early `return` (so all five sites lose their hygiene at once) and re-ran the
guard set:

```
FAILED tests/teatree_loop/phases/test_scan.py::TestScanPhaseConnectionHygiene::test_scan_phase_closes_a_worker_threads_db_connection - DID NOT RAISE
FAILED tests/teatree_core/worktree/test_readiness.py::TestRunProbesConnectionHygiene::test_probe_worker_db_connection_is_closed - DID NOT RAISE
FAILED tests/teatree_core/management_commands/test_workspace_provision_parallel.py::TestProvisionPoolConnectionHygiene::test_pool_worker_db_connection_is_closed - DID NOT RAISE
FAILED tests/teatree_loops/test_worker.py::TestExecutorThreadConnectionHygiene::test_spawned_thread_closes_its_raw_db_handle - DID NOT RAISE
FAILED tests/teatree_utils/test_thread_db.py::TestCloseThreadDbConnections::test_closes_a_worker_threads_raw_db_handle
FAILED tests/teatree_utils/test_thread_db.py::TestCloseThreadDbConnections::test_dereferences_the_wrapper_so_django_reopens
FAILED tests/teatree_loop/phases/test_scan.py::test_scan_phase_worker_pool_is_bounded - pytest.PytestUnraisableExceptionWarning: Exception ignored in: <sqlite3.Con...
```

So: yes, `TestScanPhaseConnectionHygiene` goes RED when the fix is removed — and the
**last line is the production bug itself**, reproduced locally on an innocent bystander
test. The helper was restored afterwards (`42732ccc` contains the restored version;
`grep -n "ANTI-VACUITY" src/teatree/utils/thread_db.py` must return nothing — it does).

I also independently pinned the Django behaviour in
`TestCloseThreadDbConnectionsIsNotVacuous::test_django_close_all_leaves_the_in_memory_handle_open`,
which passes — direct proof that `loops/worker.py` was genuinely leaking.

## Verification status — READ THIS, IT IS INCOMPLETE

Completed:

- Guard set green **before** the anti-vacuity probe: 1 run, 66 passed.
- Anti-vacuity probe: 2 runs, RED as expected (see above).
- `uv run ruff check` — passed. `uv run ruff format --check` — passed (3363 files).
- `uv run prek run ty-check --all-files` — passed.
- `t3 tool test-path-mirror` — passed (ratchet holds).
- Full pre-commit hook suite on commit — all passed (tach, import-linter, jscpd,
  module-health, banned-terms, BLUEPRINT gate, etc.).

- **Repeat runs, post-restore: 7 consecutive iterations, 92 passed each.** The guard set
  plus BOTH victim files (`test_run.py`, `test_retro_gate_failures.py`) under xdist. The
  loop was still going when I stopped; 7 confirmed green is the number to quote.
- Branch pushed. All pre-push gates passed, no bypass.
- **PR: <https://github.com/souliane/teatree/pull/3536>** (non-draft).

**NOT completed:**

1. `bash dev/ci-parity-fast.sh` — never run.
2. `bash dev/ci-parity.sh` — never run. CI is the check for the 93% floor.

## What the next person must do

1. Watch CI on [#3536](https://github.com/souliane/teatree/pull/3536) across **several
   shards**, not one — the failure is nondeterministic and shard-dependent, so a single
   green shard is not evidence.
2. If a shard still reds with `PytestUnraisableExceptionWarning`, the next suspect is
   `src/teatree/backends/slack/receiver.py:207` (see below). Localise it with a per-site
   guard test, not a whole-suite tracemalloc run.
3. Optionally run `bash dev/ci-parity.sh` locally if CI reds on coverage rather than on
   the warning.

## Things that did NOT work — do not retry

- **Do not cherry-pick `8a35ea35`.** It is already on `main`; the cherry-pick is empty.
- **Do not merge `fix/scanner-pool-connection-hygiene` into main.** It is the whole
  pre-squash `#3520` branch (20+ commits) plus the fix; `#3520` is already squashed onto
  `main`. Merging it would replay the entire refactor.
- **Do not trust `connections.close_all()`** as connection hygiene anywhere in this repo.
  It is a no-op under the `:memory:` test DB. Close the raw handle.
- **Do not add a `filterwarnings` ignore for `PytestUnraisableExceptionWarning`.** It
  would hide this whole class of resource-lifecycle bug. Not done, do not do it.
- A whole-suite `PYTHONTRACEMALLOC=25 uv run pytest tests/teatree_loop` run to find the
  allocation site timed out at 600s and produced nothing useful. The per-site guard
  tests are a far cheaper way to localise a leak — use those instead.
- Writing a test that *deliberately* leaks a connection (the `close_all` pin) will fail a
  random later test unless it closes the handle itself in a `finally`. Mine does now.

## Remaining suspects if the flake survives this

`src/teatree/backends/slack/receiver.py:207` spawns long-lived listener threads with no
connection hygiene. Not touched here (production-only path, not exercised under a
`TestCase`), but it is the next place to look.
