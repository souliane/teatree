# BLUEPRINT Appendix — Immutable worker generations and the sequential roller

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §4 (the `WorkerGeneration` row) and the admission-gate family around it. A runtime process started from a `teatree-factory:<sha>` image carries `TEATREE_GENERATION=<sha>` (`teatree/generation.py`); `""` is a legacy or dev process started from a source checkout, and it keeps every pre-generation behaviour.

## Registry and admission

- `WorkerGeneration` is the registry of which generation's code is live: `starting → active → draining → retired`, plus `failed`. The worker registers and activates its own generation once it holds the singleton; `boot` activates only a `starting` row, never a failed or retired one.
- Both claim paths stamp `Task.claimed_generation`. A generation whose own row is draining, retired or failed is refused by `claim_admission_block_reason` with its short sha named.
- `begin_drain` advances the quiesce fence in the same transaction, so a claim racing the drain rolls back exactly as it does for `worker_quiescing`; a drain that loses a race joins the one in progress.
- `drain_worker(generation=<sha>)` waits on that generation's stamped claims only and never writes `worker_quiescing`; `t3 worker drain --generation <sha>` drains one generation.
- A draining, retired or failed own generation is a `drain_block_reason`, like `worker_quiescing`: each of its in-flight runs interrupts itself at its next heartbeat and parks PENDING with its session id, so the drain ends in about one heartbeat and the next generation resumes the conversation. The `--drain-timeout` (default 600 s) only bounds a run that cannot checkpoint.

## Stranded drains

- The claim path — never the read path — re-opens a generation whose drain passed its `drain_deadline` with no successor serving (`WorkerGeneration.objects.reopen_stranded_drain`).
- A `starting` successor still inside its lease (ten minutes past the later of that deadline and its own start) keeps the drain closed; an expired one is marked failed.
- The check is a plain read when there is nothing to heal, so a claim poll takes only the claim window's write lock.

## No code moves in place

In an image generation the self-update scanner is not built, `reinstall_running_editable` is a logged no-op, `t3 update` refuses, and the doctor's runtime-clone drift check passes.

## The image

- `deploy/build-generation.sh <sha>` builds from `git archive <sha>`, so a dirty or moving checkout never reaches it, in two layers of `deploy/Dockerfile`:
  - `generation-base` — the interpreter, the locked runtime (`uv sync --frozen --no-dev --extra slack --no-install-workspace`) and prek in the image-local `/opt/teatree/venv`, built from the dependency inputs alone (the Dockerfile, `locked-version.sh`, `uv.lock`, every workspace member's `pyproject.toml`) and tagged `<repository>:base-<hash of those inputs>`, so a code-only commit reuses it unchanged;
  - `generation` on top — only the commit's source (root-owned, read-only at `/home/teatree/teatree`), the workspace's own packages, and `org.opencontainers.image.revision=<sha>`.
- `TEATREE_IMAGE_REPOSITORY` (default `teatree-factory`) names a registry repository: an image or base already there is pulled instead of built, and `TEATREE_PUSH_IMAGES=1` pushes what a build made. The build is idempotent on that tag and label.
- After a build it untags generation images and bases older than the newest three by build time, skipping — by image id, never by name — any image a container (running or stopped) was created from or the promoted tag names. `docker rmi` of one tag of a multi-tag image untags it even while a container runs it, so a name check would take the serving generation's own tag and with it the rollback path. Nothing is ever forced.
- The Dockerfile's default target is unchanged.

## Deploy cadence

- `.github/workflows/deploy.yml` debounces the push-triggered deploy: a push to `main` waits 300 s in a `debounce` job with its own cancel-in-progress group, so a newer push cancels the older run's wait and a merge burst lands as one deploy. `workflow_dispatch` skips the wait.
- The `deploy` job serializes on the fixed `deploy` group with `cancel-in-progress: false` and runs only after a debounce that succeeded or was skipped, so a running convergence is never cancelled. The workflow declares no top-level concurrency group, which would queue the debounce behind the deploy.

## The roll: entry and lock

- `deploy/roll.sh [<rev> [roller args]]` is the entry: it fetches, builds the generation, and runs the roller (`teatree/deploy/roll.py`) from the NEW image as the one-shot `teatree-roller` service, repeating a progress line at least every 30 s.
- It takes the deploy lock `deploy.sh` uses — flock, or on a host without it the shared `deploy/deploy-lock.sh` mkdir lock, which reclaims a lock from a dead pid or past its age, one reclaimer at a time under the symlink `<lock>.reclaim` (it names the reclaimer's pid) so none removes a lock another has just taken; waiting on a live reclaimer spends none of the reclaim budget.
- It keeps the same `<pid> <heartbeat> <deadline>` record `deploy.sh` keeps; the deadline covers the `--drain-timeout` the roller receives (read in base 10).
- `t3 deploy roll --to <sha>` refuses unless the record beating in the lock (read through the roller's read-only `/host-tmp` mount) carries the pid `roll.sh` passed as `TEATREE_ROLL_RECORD_PID`, so a roll never runs outside its own `roll.sh`, nor during another convergence.

## The roll: what N is

- N is what the worker CONTAINER carries — its revision label, running or stopped; the registry is read only when no worker container exists. Every other `active` row is failed as displaced, since a `deploy.sh` run replaces containers without touching the registry.
- A roll to the generation that already runs is finished (bring up what is missing, verify, promote, retire) only when that generation's row is `active` or `draining`. Otherwise — say it was failed after a verify failure that followed a migrating init — it is registered again and restarted through the full sequence, so its worker boots and activates it; a failure of that restart marks it failed and re-opens admission, since nothing older is left to restore.

## The roll: sequence

1. **Preflight** — the image exists, its label matches, and its code is not behind the applied schema (the roller runs from the target image, so its own process freshness is the target's).
2. **Register N+1** — a failed, retired or draining row starts again.
3. **Drain N** — per generation when N is a registered active or draining generation; otherwise the global drain, which reopens before N+1 comes up.
4. **Stop** N's worker and Slack listener.
5. **Init** — run N+1's `init`.
6. **Up** — N+1's worker, listener, admin and watchdog.
7. **Verify** — every required runtime container carries N+1's label and kept one container start for `--stable-seconds`, the worker activated N+1, the admin answers inside its own container; `--optional-service` declares a service the stack legitimately runs without, never the worker or the admin.
8. **Promote** — retag the promoted tag, `teatree-headless:latest` by default.
9. **Retire** N.

## Failure and rollback

- A failure before N's drain starts marks N+1 failed, leaves N untouched (legacy or generation) and re-raises.
- After it, any failure — or a SIGTERM/SIGHUP, which the CLI turns into `RollInterruptedError` — rolls back: N+1 is marked failed (a failure to mark it never aborts the restore), N is reinstated (a draining row resumes, a failed or retired one starts again), brought back up from its own image and must pass the same verification N+1 had to (a legacy stack: every runtime container running and the admin answering); a restore that does not verify fails loud.
- The roller snapshots the applied migrations before N+1's `init`; a failure after that schema moved is never rolled back — N's code would refuse every claim against it (`code_behind_schema`) — so the roll marks N+1 failed, leaves the stack as it is and exits 1 naming the migrations; the fix is a roll forward (or a re-roll of the same sha once it is fixed, which restarts it as above).
- Promotion (one retry) follows a successful verify and never undoes it: a tag that cannot move leaves N+1 serving and N draining and exits 1; a re-run to the serving sha brings up missing services, verifies, promotes and retires what is left.
- After the first termination signal a roll ignores further ones until its restore finishes. An interrupted roll says what it left: interrupted before anything moved (exit 1), rolled back (exit 3), stopped before the drain with N still serving (exit 3), or interrupted after N+1 verified, when N+1 serves unpromoted (exit 1, re-run to finish).

## First cutover

The first roll off a legacy stack finds no registry table: it drains and stops the legacy stack, lets N+1's init create the table, and registers N+1 only then, so a `t3 worker drain --generation <serving sha>` never strands the factory.

## Topology

- The roller reaches containers through a `ComposeEngine` Protocol (`teatree/deploy/compose_engine.py` over the docker CLI), which brings each generation up with the compose topology baked in that generation's own image and addresses everything else by compose labels.
- `deploy/docker-compose.yml` stays the source-mounted stack `deploy.sh` and `deploy/t3` run; its one change is that the watchdog's `/host-tmp` bind reads `${TEATREE_HOST_TMP:-/tmp}`, so its default render is identical.
- The image-only layout is the override `deploy/docker-compose.generation.yml`, which only the roller and a generation's own watchdog add, on top of the base read out of the same image and before `docker-compose.host-identity.yml`: every runtime role runs the image's own `/usr/local/bin/entrypoint.sh` with `pull_policy: never`, the build reset, no source or `teatree_uv` mount and no `TEATREE_CLONE_DIR` (the image sets it), and `env_file` addressed through `TEATREE_DEPLOY_CHECKOUT`.
- Compose drops a list entry only by replacing the list, so the override repeats the base volumes minus those two mounts and `tests/test_deploy_generation_image.py` fails on any drift.
- `roll.sh` refuses a deploy checkout at or around the baked tree, which its path-identity bind would shadow.

## The baked tree

- The build stamps the archived root with `.teatree-generation` (the commit sha); `teatree.paths.PathHelpers.is_baked_generation_root` recognises it, so `find_project_root` and `t3 setup`'s `find_main_clone` resolve a baked tree — core-only or a fork with core at `vendor/teatree` — without a `.git`, while a checkout still needs one. The marker counts only when it names the running `TEATREE_GENERATION`.
- `runner_prefix` runs an overlay's `manage.py` inside a baked tree with the image's own interpreter instead of `uv run`, which would try to create a `.venv` in the read-only tree.
- Init migrates before `t3 setup`, which reads the config table. With `TEATREE_GENERATION` set, the entrypoint's init skips the clone refresh, the constraints export, every `uv` install and `prek install`, and the watchdog execs the watchdog baked in the image, which repairs with the generation override.

## One-offs and second stacks

- `deploy/generation-topology.sh` is the one reader of a generation's topology (base + override + host identity when the host home differs) for `roll.sh`, the watchdog and `deploy/t3`. Every read passes `docker run --pull never`; a failed read leaves nothing half-written and stops with a message naming the file and the image.
- `deploy/t3`'s one-off containers run the image the service's container was created from (`.Config.Image`), or the promoted tag when none exists, with that generation's topology whenever it carries a revision label.
- A rollback to the legacy stack uses the checkout's compose with `TEATREE_CLONE_DIR` set from `TEATREE_LEGACY_CLONE_DIR`, which `deploy/roll.sh` derives from the layout.
- A second stack can be rolled on the same daemon: `TEATREE_COMPOSE_PROJECT`, `TEATREE_PROMOTED_TAG`, `TEATREE_ADMIN_PORT` (the admin bind and the roller's probe) and `TEATREE_DEPLOY_LOCK` (which must sit under `TEATREE_HOST_TMP`, where the watchdog and the roller read it) default to the live stack's `teatree`, `teatree-headless:latest`, 8000 and `/tmp/teatree-deploy.lock`.

## Known limits

- **A crashed reclaimer leaves a narrow race.** A reclaimer that dies while holding `<lock>.reclaim` leaves the symlink behind. It is abandoned once its pid is dead or it is a minute old. Two reclaimers that find it abandoned at the same instant can both remove it and both enter the critical section, which is the original check-then-remove race, narrowed to that coincidence (`deploy/deploy-lock.sh`, `_take_reclaim_mutex`).
- **Image protection reads only this host's containers and this shell's promoted tag.**
  - An image held only by a leftover stopped container is never pruned until that container is removed.
  - A second stack with no containers left loses protection of its own promoted tag unless `TEATREE_PROMOTED_TAG` names it in the shell that runs `build-generation.sh`.
- **On a host without flock, a SIGKILLed roll.sh leaves its roller running.** Another roll can then reclaim the mkdir lock from the dead pid while that roller still works. On hosts with flock the kernel releases the lock only when every holder exits, so they are not exposed. The lock's record also stops beating, which is what the watchdog and `t3 deploy roll` read.
