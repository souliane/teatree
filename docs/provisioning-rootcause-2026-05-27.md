# Provisioning paradigm root-cause — 2026-05-27

## 1. Root cause (one paragraph)

Teatree's `OverlayBase` is a duck-typed Python ABC where every extension point except `get_repos` and `get_provision_steps` ships a **silent no-op default** (`return []`, `return {}`, `return False`, `return None`). Provisioning itself is a flat ordered list of `ProvisionStep` callables (`teatree.types.ProvisionStep` — just `name`/`callable`/`required`/`description`), executed by `run_provision_steps` and reported on the *callable's return value*, not on a typed post-condition. The Worktree FSM (`Worktree.provision` → `services_up` → `verify` → `ready`) advances on "the runner returned `ok=True`", which itself is "every required callable did not raise". That is the entire integrity surface. There is no declared `produces` set, no `requires` set, no `post_condition` probe, and no schema validation of overlay-supplied config. Consequences: an overlay can forget `uses_redis`/`get_required_ports`/`get_db_import_strategy`/`get_readiness_probes` and the lifecycle reports green; a step (`install-custom-requirements`, `compose-override`, `npm-install`, `ensure-superuser`) can "succeed" while its real artifact is broken (symlink dangling inside container, env file unreadable, DB dropped out-of-band, node_modules empty); and the next step crashes 4 minutes later on a wrong layer. Every recent fix is therefore a special case bolted onto a step's callable (try/except → normalize-to-success, skip-if-not-Django, map-variant-A-to-variant-B, pipe-via-stdin) — never a tightening of the contract. The contract is incapable of holding the invariants the patches encode, so the invariants leak back out the next time a new repo/variant/host condition shows up.

## 2. Recurrence patterns

### Pattern A — "Hook left at no-op default, silently degrades runtime"

Default `get_health_checks`/`get_readiness_probes`/`get_required_ports`/`get_db_import_strategy`/`uses_redis` all return falsy and the lifecycle continues.

- [souliane/teatree#23](https://github.com/souliane/teatree/issues/23) (port/redis/health hooks not overridden by a downstream overlay — needed retrofit).
- A downstream overlay had `get_db_import_strategy` returning a strategy for FE/translations repos with no DB.
- [souliane/teatree#787](https://github.com/souliane/teatree/issues/787) (still **OPEN**) — "No conformance guard for registered-overlay extension-point signature drift".
- [souliane/teatree#1144](https://github.com/souliane/teatree/issues/1144) (closed) — BLUEPRINT drifted: docs missed 16 OverlayBase hooks.

### Pattern B — "Step `callable` returns, but post-condition is false"

`ProvisionStep` has no post-condition field.

- [souliane/teatree#1313](https://github.com/souliane/teatree/issues/1313) / PR [#1316](https://github.com/souliane/teatree/pull/1316) — `.env` symlink unreadable inside container; cascades to install-custom-requirements failing.
- A downstream `ensure-superuser` step had to special-case "is already taken" as success.
- A downstream `npm-install` symlink health check passed on an empty `node_modules`.
- A downstream `reset-passwords` step failed with "is a directory" docker error; required a skip-flag.
- [souliane/teatree#480](https://github.com/souliane/teatree/issues/480) / PR [#483](https://github.com/souliane/teatree/pull/483) — symlink health check passed when target dir was already populated.
- [souliane/teatree#484](https://github.com/souliane/teatree/issues/484) / PR [#485](https://github.com/souliane/teatree/pull/485) — spurious "DB import failed" for repos without a database.

### Pattern C — "Variant/tenant mapping leaks into every call site"

- Multiple downstream issues — a child-variant aliasing miss recurred in 2 separate places one week apart.
- [souliane/teatree#1322](https://github.com/souliane/teatree/issues/1322) — "worktree not DB-linked to backend; local creds map missing a child-variant alias".

### Pattern D — "FSM advances on callable return, not on truth"

- [souliane/teatree#1374](https://github.com/souliane/teatree/issues/1374) (**OPEN**, filed today) — worktree status reports `provisioned` when the Postgres DB doesn't exist.
- [souliane/teatree#1201](https://github.com/souliane/teatree/issues/1201) (**OPEN**) — "Resilience: verify-on-transition".
- [souliane/teatree#390](https://github.com/souliane/teatree/issues/390) (**OPEN**) — "make silent subprocess failure + state drift ungrammatical".

### Pattern E — "Squatter / shared singleton reconciliation"

- [souliane/teatree#1373](https://github.com/souliane/teatree/issues/1373) (**OPEN**, filed today) — `teatree-redis` can't reconcile when a non-teatree container squats on 6379.

## 3. Structural fix (the contract, not another patch)

### 3.1 Pydantic `OverlayConfig` with fail-closed defaults

Move `OverlayBase`'s hook surface into a Pydantic model. Required fields raise at overlay load time.

### 3.2 `ProvisionStep` becomes a DAG node

```python
@dataclass(frozen=True)
class ProvisionStep:
    name: str
    callable: Callable[[ProvisionContext], None]
    requires: frozenset[str]
    produces: frozenset[Artifact]
    post_condition: Probe
    idempotent: bool
```

Step success = `(callable returned without raising) AND (post_condition probe passes)`.

### 3.3 FSM transitions gated by aggregate post-condition

`PROVISIONED` only if every step's `post_condition` holds. `services_up` requires every declared `PortPublished` and `ContainerHealthy`. `ready` requires every `Probe` from `get_readiness_probes`.

### 3.4 Single source of truth for port allocation

`PortLedger` Django model owning every host port — teatree-redis, Postgres, compose-allocated.

### 3.5 Variant as a first-class type

`Variant` dataclass with `canonical_tenant`, `default_language`, `dslr_snapshot_name`, `e2e_credentials_key`.

## 4. PRs / open issues to STOP or redirect

1. [souliane/teatree#878](https://github.com/souliane/teatree/issues/878) — typed DB-provisioning FSM — redirect to subsume the whole provision-DAG contract (§ 3.2 / § 3.3).
2. [souliane/teatree#962](https://github.com/souliane/teatree/issues/962) — RAM auto-throttle — hold until DAG runner is in place.
3. [souliane/teatree#1308](https://github.com/souliane/teatree/issues/1308) / merged PR [#1345](https://github.com/souliane/teatree/pull/1345) — overlay-provision-smoke — pair with `tests/conformance/test_overlay_contract.py` for the silent-no-op-hook class.
4. A downstream `slow_import` recorded-approval issue — fold into § 3.1 `OverlayConfig` Pydantic move.

## 5. Falsification experiment (run today, ~1 hour)

1. `tests/conformance/test_overlay_default_noops.py` — every registered overlay must override every `OverlayBase.get_*` hook with falsy default.
2. `tests/conformance/test_step_post_conditions.py` — zero steps can complete without a post-condition probe artifact.
3. Drop a worktree DB, run the overlay's worktree-status command — must report non-zero / `db-missing`.
4. Stop `teatree-redis`, run an interloper container holding 6379, then `t3 infra redis up` — must evict or exit non-zero.
5. `rm` `.t3-cache/.t3-env.cache` on a `provisioned` worktree — some probe must refuse green.

If any of 1, 2, 3, 5 passes on main without code changes → paradigm-mismatch overstated. If 4 of 5 fail → § 3 contract is the exit.

## Critical files

- `src/teatree/core/overlay.py`
- `src/teatree/types.py`
- `src/teatree/core/step_runner.py`
- `src/teatree/core/runners/worktree_provision.py`
- The relevant downstream overlay's provisioning module (in its own private repo).
