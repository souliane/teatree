# teatree headless deployment

Run teatree headless on a single existing box, reached over direct SSH, driven
by one GitHub Action. teatree runs the autonomous loop (`t3 worker`,
`agent_runtime=headless`), self-updates via its own loop, autostarts on reboot,
and serves the Django admin on the box loopback for SSH-tunnel access. The image
is **self-contained** — it bakes a pinned source@ref + interpreter + locked deps
so a fresh box boots deterministically and offline; see
[Self-contained image](#self-contained-image--bake--publish-3451).

This is **teatree-only** — no customer or product overlays. The only registered
overlay is the built-in `t3-teatree`.

## How the one-click deploy works

`Actions → Deploy teatree → Run workflow` (`.github/workflows/deploy.yml`):

1. Connects to the box at the fixed host/IP held in the `DEPLOY_SSH_HOST` secret
   (masked out of the logs immediately) — no cloud API resolves it, so any
   directly-reachable box works, not just a Hetzner Cloud one.
2. Writes the SSH key + known_hosts from secrets, connects with strict host-key
   checking.
3. Writes the box secrets file `deploy/teatree.env` over SSH (piped over stdin,
   mode 600 — never on a command line or in logs).
4. Runs `deploy/deploy.sh` on the box, which brings the checkout current and runs
   `docker compose up -d --build`.

The stack is five services from one image (`deploy/Dockerfile`), selected by
`TEATREE_ROLE`:

| Service | Role | Restart | Notes |
| --- | --- | --- | --- |
| `teatree-init` | one-shot prep | `no` | clone + editable install + `t3 setup` + DB config; exits 0 |
| `teatree-worker` | `t3 worker` | `unless-stopped` | the loop cadence owner; `DEBUG` off; CPU/RAM caps derived from the host at deploy time (see [Worker sizing](#worker-sizing-derived-from-the-host)) |
| `teatree-admin` | `t3 admin` | `unless-stopped` | Django admin under gunicorn on the box loopback `127.0.0.1:8000` (host networking); `DEBUG` off |
| `teatree-slack-listener` | `t3 slack listen` | `unless-stopped` | Socket-Mode receiver; harmless no-op-retry on a box with no Slack overlay |
| `teatree-watchdog` | `watchdog.sh --loop` | `always` | the in-daemon self-heal sidecar (docker socket + read-only checkout; no secrets); see [Self-heal watchdog](#self-heal-watchdog-h24-owner-directive-10) |

`worker` and `admin` wait for `init` to complete, so the editable install on the
shared clone happens exactly once. All three mount the same state, so the admin and
the worker read the **same** `db.sqlite3` (WAL — safe concurrent reads):

| Mount | Path | Kind | Holds |
| --- | --- | --- | --- |
| `teatree_src` | `/home/teatree/teatree` | named volume | the teatree clone (source) |
| DB dir | `/home/teatree/.local/share/teatree` | **host bind mount** | the canonical DB (`db.sqlite3`) + backups |
| worktrees | `/home/teatree/.local/share/teatree-worktrees` | **host bind mount** | per-worktree isolated DBs |
| workspaces | `/home/teatree/workspace/t3-workspaces` | **host bind mount** | ticket worktrees |
| pass store | `/home/teatree/.password-store` | **host bind mount** | the gpg-encrypted secret store (Anthropic OAuth token, …) |
| GPG home | `/home/teatree/.gnupg` | **host bind mount** | the private key that decrypts the pass store |
| `teatree_uv` | `/opt/teatree/uv` | named volume | the runtime teatree Python + venv + `t3` shims |

The **credential plane** (`~/.password-store` + `~/.gnupg`) is a dedicated pair of
bind mounts, deliberately decoupled from the data dir: the container's
`PASSWORD_STORE_DIR`/`GNUPGHOME` point at these canonical host paths, so a future
change to the data-dir mount can never orphan the provisioned credential store (as
happened when #3262 moved the data dir to a bind mount while these paths still
pointed inside it). Secrets stay gpg-encrypted on the host disk, outside the
backed-up data dir. A box using the `CLAUDE_CODE_OAUTH_TOKEN` env path instead just
leaves these dirs empty (`deploy.sh` pre-creates them owned by the deploy user).

### Worker sizing: derived from the host

The worker reads its own **cgroup-capped** CPU/RAM view, so a host-derived
provision-concurrency default (`nCPU/2`) is a no-op unless the cgroup cap itself
reflects the host. So `deploy/deploy.sh` — which runs **uncapped on the host** —
reads the real host cores/RAM via `src/teatree/utils/ram_probe.py` and exports
`TEATREE_WORKER_CPUS` / `TEATREE_WORKER_MEM_LIMIT` before `docker compose up`. The
compose worker service interpolates them (`cpus: "${TEATREE_WORKER_CPUS:-3.0}"`,
`mem_limit: "${TEATREE_WORKER_MEM_LIMIT:-18g}"`), so the cgroup cap tracks the box
and `available_cpu_count` inside the worker derives concurrency from the host, not
a baked-in 3-core cap. When `python3` is absent or RAM is unreadable — or on a bare
`docker compose up` — the in-file defaults (`3.0` / `18g`, sized for a ~30GB host)
apply. The watchdog's `up -d --no-recreate` does not re-size a running worker; the
next deploy re-asserts the derived caps.

### External DB — one DB on the host disk

The DB, worktrees, and workspaces are **host bind mounts at their canonical
absolute paths**, not Docker-internal named volumes. The bind source and the
container target are the identical path (path identity — `deploy/Dockerfile` sets
no `XDG_DATA_HOME` and HOME is `/home/teatree` in both the container and the box),
so the container and the host converge on **one** `db.sqlite3` on the host's
daily-backed-up disk. There is no separate Docker-internal factory DB to drift
from the operator's real DB.

`teatree_src` and `teatree_uv` stay Docker-managed named volumes for now (later
PRs handle code-mount modes).

### UID invariant — the container user must equal the host deploy user

Every state, credential, and session mount in the table above is a **host bind
mount at path identity**, so each file on the host disk carries the host deploy
user's numeric UID while the container writes it as the image's `teatree` user.
Those two must be the **same UID**, or every bind mount is unwritable from inside
the container and `init` crash-loops on its first write.

`deploy.sh` therefore **derives the container UID from the host at deploy time**:
it runs as the deploy user, reads that user's UID (`id -u`), and exports it as the
`TEATREE_UID` build arg (compose reads `${TEATREE_UID}` into the image build). The
image build then renumbers its `teatree` user onto that UID and removes Ubuntu
24.04's stock `ubuntu` user (which occupies UID 1000) so the id is free. Because the
container UID is taken from whatever box runs the build, **the invariant holds on
any box with no manual step** — the live box (deploy user 1001) rebuilds at 1001
with no chown, and a fresh box lands on its own deploy user's UID.

The default UID, used when nothing exports `TEATREE_UID` (the `ARG TEATREE_UID`
default in `deploy/Dockerfile`, and the `${TEATREE_UID:-1001}` default in
`deploy/docker-compose.yml`), is **1001** — the live box's deploy user, and the UID
`useradd teatree` lands on unaided inside the image (UID 1000 is taken by the stock
`ubuntu` user, so `teatree` takes the next free id, 1001). `deploy.sh` also
pre-creates every bind-mount source owned by the deploy user before the stack
starts.

A bare `docker compose build` (no `deploy.sh`) uses that 1001 default. To build for
a different deploy user by hand, export or pass the UID explicitly:

```bash
TEATREE_UID="$(id -u)" docker compose -f deploy/docker-compose.yml build
# or: docker compose -f deploy/docker-compose.yml build --build-arg TEATREE_UID="$(id -u)"
```

Changing the container UID on a box that already has bind-mount data written under
the old UID requires a one-time `chown` of the bind sources to the new UID (stack
stopped), otherwise the pre-existing files stay unwritable.

## Self-contained image — bake + publish (#3451)

The image is **self-contained**: `deploy/Dockerfile` bakes a pinned source@ref +
the managed Python 3.13 interpreter + the locked editable `t3` install (the same
`uv sync --locked` reproducibility pattern as `dev/Dockerfile.test`, extended to
also bake the source and the tool). So a fresh box with only the image + secrets
boots deterministically and **offline** — first boot no longer clones the repo or
cold-resolves the dependency graph from github/PyPI/astral.

**How the two named volumes inherit the bake.** Docker seeds a *fresh* named
volume from the image content at the mount path, so on a brand-new box the empty
`teatree_src` → `~/teatree` and `teatree_uv` → `/opt/teatree/uv`
volumes are seeded with the baked clone, interpreter, and tool. An
already-provisioned box keeps its populated volumes (the bake is shadowed) and
converges via the runtime clone's origin fast-forward — so the bake changes only
**fresh-box** first boot; existing boxes are unaffected.

**Boot mode — online vs. offline (`deploy/entrypoint.sh`).** The entrypoint picks
its mode from a bare origin reachability probe (`network_up`):

- **online** — fast-forward the runtime clone from origin and refresh the editable
  install; `t3 update` remains the in-loop self-update path;
- **offline** — run the baked snapshot as-is, with zero fetches. Set
  `TEATREE_FORCE_OFFLINE=1` to force this pinned no-fetch boot deliberately.

Note the GitHub **token preflight** (`assert_gh_token_permissions`) still contacts
`api.github.com`, so a fully air-gapped box cannot pass init — the loop needs
GitHub to function. The bake removes the *source + dependency* fetches (the
github/PyPI/astral blips that made first boot non-deterministic), not the loop's
own GitHub access.

### Pulling a published image instead of building on the box

`docker-compose.yml` resolves the image as `${TEATREE_IMAGE:-teatree-headless:latest}`.
Leaving `TEATREE_IMAGE` unset builds locally from `deploy/Dockerfile` (the current
on-box flow, unchanged). To PULL a published, self-contained tag, set
`TEATREE_IMAGE` to the registry ref in a `.env` file next to `docker-compose.yml`
(or in the deploy shell):

```bash
# deploy/.env  (compose-interpolation env, NOT the container env_file teatree.env)
TEATREE_IMAGE=ghcr.io/<owner>/teatree-headless:<lockkey>-<shortsha>
```

`.env` adjacency keeps the value identical for the host deploy AND the
path-identity-mounted watchdog, so both resolve the same image; the watchdog's
`up -d --no-recreate` never recreates a running container on a value change anyway.

### Publishing the self-contained image

`.github/workflows/publish-image.yml` builds `deploy/Dockerfile` (pinning
`--build-arg TEATREE_SOURCE_REF=<commit>` to the built commit) and pushes it. Tags:

- `<lockkey>-<shortsha>` — reproducible primary tag, keyed on the baked toolchain
  (`deploy/Dockerfile` + `deploy/entrypoint.sh` + `uv.lock` + `pyproject.toml`)
  **and** the source commit;
- `sha-<shortsha>` — the exact source commit;
- `latest` — main's tip (float on it, or pin a primary tag for reproducibility).

**Registry is configurable. The default needs no owner-provisioned secret:** it
publishes to **ghcr.io** authenticated with the workflow's built-in `GITHUB_TOKEN`
(the same pattern CI already uses for its test image) at
`ghcr.io/<owner>/teatree-headless`. To publish elsewhere, the owner sets:

| Kind | Name | Purpose | Needed when |
| --- | --- | --- | --- |
| Variable | `TEATREE_IMAGE_REGISTRY` | registry host (default `ghcr.io`) | non-ghcr registry |
| Variable | `TEATREE_IMAGE_NAME` | image path (default `<owner>/teatree-headless`) | custom image name |
| Secret | `TEATREE_REGISTRY_USER` | registry login user | non-ghcr registry |
| Secret | `TEATREE_REGISTRY_TOKEN` | registry login token/password | non-ghcr registry |

On a fork PR, or a custom registry with no `TEATREE_REGISTRY_TOKEN`, the workflow
runs **build-only** (proves the image builds, pushes nothing). For the default
ghcr path the repo/org must allow the package (GHCR is enabled by default on
GitHub-hosted repos); a first publish creates the package, which the owner can
then make public or keep private (a private package still pulls on the box with a
`packages: read` token).

### One-time volume migration (operator step)

A box that ran an older deploy has its state in Docker named volumes
(`teatree_teatree_data`, `teatree_teatree_worktrees`, `teatree_teatree_workspaces`).
`deploy/migrate-volume-data.sh` is a **one-time, idempotent, operator-run** step
that moves that real factory state onto the host bind paths before the stack
switches to bind mounts. Run it once, with the stack stopped:

```bash
docker compose -f deploy/docker-compose.yml down   # the script refuses while up
sudo deploy/migrate-volume-data.sh                 # sudo: reads /var/lib/docker/volumes
```

It archives the existing host DB + backups (timestamped, never deleted) before
overwriting them with the container volume's copy (the real factory state), then
copies the credentials, worktrees, and workspaces across and brings the stack up.
A fresh box with no prior volumes does not need this step.

Re-running the deploy workflow is **idempotent** — it converges the same stack.

## Fleet role split — which loops run where

teatree runs as a small fleet: the operator's laptop plus this headless box.
Some autonomous loops are **fleet-scoped** — they must run on exactly **one**
instance or they double-act. The box now **hosts the DM-only Slack conversational
loop** for the owner overlay, so `inbox` (the inbound-messaging scanners: Slack DM
→ `PendingChatInjection`, review-intent, red-card, mentions) runs here and drives
the headless drain → 👀-ack → answer cycle. Only the **colleague-facing** Slack
loops stay on the laptop, since running them here would both misfire and duplicate
the laptop's. The split:

| Instance | Runs | Does not run |
| --- | --- | --- |
| **Laptop** | `review` (colleague PR review → Slack), `directive_loop` (asks the human via Slack) | `tickets` — the operator disables it by hand |
| **Box** (this deploy) | `inbox` (DM-only owner Slack loop), `tickets` (issue scanning/dispatch) + all machine-local loops | `review`, `directive_loop` |

The box enforces its side in `deploy/entrypoint.sh` (init role) via
`apply_fleet_loop_policy`, after seeding config. Per-loop enable/disable is now
EMERGENCY-only (#3248) and, more importantly, admission resolves
`hold > forced > preset > base` — so no preset, schedule, or `t3 loop override`
can revive a loop a prior deploy left in a durable `LoopState` **hold** (older
images ran `t3 loop disable inbox`). The step therefore drives the two
authoritative planes:

- **ENABLED set** (default `inbox`) → `t3 loop enable <name> --emergency`, the one
  handle that clears a stale hold and sets `Loop.enabled=True`, so a box whose
  `inbox` an older image disabled recovers. Idempotent.
- **DISABLED set** (default `review,directive_loop`) → `t3 loop override <name> off`,
  the sanctioned NON-emergency forced-off that replaces the deprecated
  `t3 loop disable`. Forced-off beats the preset mask and the base config, so the
  colleague/human-facing loops stay off here under any mode. Idempotent.

`TEATREE_ENABLED_LOOPS` / `TEATREE_DISABLED_LOOPS` (comma-separated) override the
defaults; an **empty** value acts on nothing. Every name in both lists is
validated against the registered mini-loops first, so a typo fails the deploy
loudly. Set them in the box env file (`teatree.env`) to change the split. Because
the deploy workflow rewrites `teatree.env` from repository variables on every run,
a persistent override belongs in that workflow's env-file writer (the
`TEATREE_ENABLED_LOOPS` / `TEATREE_DISABLED_LOOPS` lines), not a hand-edit on the box.

## One-time bootstrap (on the box)

Do this once as an admin on the box.

1. **Create a dedicated deploy user in the docker group** and install its SSH
   public key:

   ```bash
   sudo adduser --disabled-password --gecos "" teatree
   sudo usermod -aG docker teatree
   sudo -u teatree mkdir -p /home/teatree/.ssh
   # add the deploy PUBLIC key to authorized_keys (paste the .pub contents):
   sudo -u teatree tee -a /home/teatree/.ssh/authorized_keys < deploy-key.pub
   sudo -u teatree chmod 700 /home/teatree/.ssh
   sudo -u teatree chmod 600 /home/teatree/.ssh/authorized_keys
   ```

2. **Install git and Docker Engine** (the compose plugin ships with Docker) and
   enable Docker on boot. git is needed for the box-side checkout that `deploy.sh`
   keeps current:

   ```bash
   sudo apt-get update && sudo apt-get install -y git
   curl -fsSL https://get.docker.com | sudo sh
   sudo systemctl enable --now docker
   ```

   (`deploy.sh` also runs `systemctl enable --now docker` if it isn't active; give
   the deploy user passwordless sudo for that one command if you want the workflow
   to self-heal it, otherwise this bootstrap step covers it.)

3. **Clone the build context** for the deploy user (the workflow also clones it if
   absent):

   ```bash
   sudo -u teatree git clone https://github.com/souliane/teatree.git /home/teatree/teatree-deploy
   ```

   The deploy always builds the checkout's current branch (default `main`).

### GitHub repository secrets to set

| Secret | Purpose |
| --- | --- |
| `DEPLOY_SSH_HOST` | the box host/IP to SSH into directly (e.g. `82.25.60.50`) |
| `DEPLOY_SSH_USER` | the deploy user (e.g. `teatree`) |
| `DEPLOY_SSH_PORT` | the SSH port |
| `DEPLOY_SSH_KEY` | the deploy **private** SSH key |
| `DEPLOY_SSH_KNOWN_HOSTS` | the box's host key line(s) for strict checking |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token — the headless auth |
| `T3_ADMIN_USER` | Django admin superuser name |
| `GIT_AUTHOR_NAME` | git identity for the loop's commits |
| `GIT_AUTHOR_EMAIL` | git identity email (use a GitHub noreply for public repos) |

The **GitHub token** and the **admin password** are **not** repository secrets and
are **never** written to `teatree.env`. They live in the box's `pass` store and are
sourced at boot — see [Credential provisioning](#credential-provisioning-passgpg)
below (#3454, #3433). `CLAUDE_CODE_OAUTH_TOKEN` is optional too when the box is provisioned with
`anthropic/<account>/oauth-token` entries.

The `known_hosts` line comes from `ssh-keyscan -p <port> <box-host>` (run once,
verify the fingerprint out of band).

### Credential provisioning (pass/GPG)

No plaintext GitHub token or admin password ever lands on the box disk. Both live
in the box's gpg-encrypted [`pass`](https://www.passwordstore.org/) store — the same
credential plane (`~/.password-store` + `~/.gnupg`, bind-mounted into every app
service) that holds the Anthropic OAuth tokens — and `deploy/entrypoint.sh` sources
them into the environment at boot (`source_secret_from_pass`), before the token
preflight and `t3 setup`.

| Boot env var | Default `pass` path | Override |
| --- | --- | --- |
| `TEATREE_GH_TOKEN` | `github/souliane/pat` | `TEATREE_GH_TOKEN_PASS_PATH` |
| `T3_ADMIN_PASSWORD` | `teatree/admin-password` | `T3_ADMIN_PASSWORD_PASS_PATH` |

An existing env value always wins; the store is the fallback. So a box that still
carries a literal in `teatree.env` keeps working, and a missing `pass` entry is a
no-op (the `TEATREE_GH_TOKEN` preflight then fails loud; a missing admin password
just yields a generated one, since loopback auto-login — not the password — is the
admin boundary).

**One-time provisioning on the box** (as the deploy user, needs host access once):

```bash
# 1. A GPG keypair to encrypt the store (no passphrase, or a gpg-agent-cached one,
#    so `pass show` runs non-interactively on a headless box):
gpg --batch --gen-key <<'EOF'
%no-protection
Key-Type: eddsa
Key-Curve: ed25519
Subkey-Type: ecdh
Subkey-Curve: cv25519
Name-Real: teatree deploy
Name-Email: teatree@localhost
Expire-Date: 0
%commit
EOF

# 2. Initialise the pass store against that key, then insert the secrets
#    (`-m` reads from stdin so the value never hits argv or shell history):
pass init teatree@localhost
printf '%s' "<github-pat>"      | pass insert -m -f github/souliane/pat
printf '%s' "<admin-password>"  | pass insert -m -f teatree/admin-password
# Anthropic tokens follow the same store, one per account:
printf '%s' "<oauth-token>"     | pass insert -m -f anthropic/<account>/oauth-token
```

The `TEATREE_GH_TOKEN` needs a **required** set and a **recommended** set of
permissions. `init` preflights both (#3405, #3436, #3477):

- **Required** — `metadata: read`, `issues: write`, `pull_requests: write`,
  `contents: write`. A gap here **fails the deploy loud** (`exit 1`): the loop
  cannot boot without them. A classic PAT satisfies the whole set with the
  single `repo` scope.
- **Recommended** — `workflows: write` (pushing a PR that touches
  `.github/workflows/*`), `actions: write` (`t3 eval ci-trigger`'s `gh workflow
  run` dispatch), `actions: read` (`t3 eval ci-status`'s `gh run list/view/
  download`), `checks: read` (the required-checks rollup auto-merge reads —
  **strongly recommended**, auto-merge fails closed without it), `statuses:
  read` (legacy commit-status rollup completeness), and `projects: read`
  (GitHub Projects v2 board sync, probed only when the overlay configures a
  board). A gap here **only WARNs** — the deploy still boots and the gated
  feature simply degrades (a CI-eval command errors, auto-merge treats the
  rollup as not-yet-green, board sync no-ops). A classic PAT adds this set
  with the `workflow` and `read:project` scopes alongside `repo`.

GitHub exposes no API to widen an existing token's scopes/permissions — the
preflight's remediation is always "recreate the token with the right grant".
For a classic PAT, create a new one with every scope teatree uses:
`https://github.com/settings/tokens/new?scopes=repo,workflow,read:project&description=teatree`.
For a fine-grained PAT, recreate it at
`https://github.com/settings/personal-access-tokens` with the missing
permission(s) the WARN names added — a fine-grained token cannot be widened
via the API either.

`workflows: write` is the one permission the fine-grained preflight cannot
actively probe (GitHub enforces it specifically on `.github/workflows/*`
writes, and the route-level 403-vs-404 ordering for a denied token could not
be confirmed without a restricted token to test against — see the gate's
module docstring for the full reasoning). It is always listed in the WARN
output so the operator verifies it manually; a classic PAT's `workflow` scope
is checked deterministically instead.

**Secrets-only rotation** — no SSH edit of `teatree.env`, no host file surgery:

```bash
# Update the entry in the pass store (host access to run `pass`, one command):
printf '%s' "<new-github-pat>" | pass insert -m -f github/souliane/pat
```

Then re-run **Actions → Deploy teatree** (or restart the stack): the
entrypoint re-sources the rotated value from `pass` at boot. Because the token is no
longer written into `teatree.env`, rotation is a `pass` update plus a redeploy —
the deploy workflow never rewrites the on-disk secret file for it.

## Access & networking

- **Admin (Django under gunicorn):** the admin binds to the box loopback only (host
  networking, `DEBUG` off). Reach it through an SSH tunnel — no port is exposed
  publicly:

  ```bash
  ssh -L 8000:localhost:8000 -p <ssh-port> <ssh-user>@<box-host>
  # then open http://localhost:8000/admin
  ```

  There is **no admin login prompt**: the auto-login middleware authenticates the
  first superuser on a `/admin/` request, but only when BOTH the
  `admin_autologin_enabled` setting is on (the init role seeds it `true`) AND the
  request originates from loopback. The box binds the admin to its real loopback,
  so the SSH-tunnelled request arrives as `127.0.0.1` and clears the loopback
  check — the **SSH tunnel to the loopback, not an admin password, is the security
  boundary**. A non-loopback request is never auto-logged-in, even with the flag
  on, so exposing the port off-loopback cannot open the admin. `T3_ADMIN_USER` /
  `T3_ADMIN_PASSWORD` still seed that superuser row deterministically (they matter
  as a password only when auto-login does not apply). Because the boundary is
  *loopback identity*, any same-host process — or a same-host reverse proxy — that
  reaches `127.0.0.1:8000` is trusted as superuser, so keep the box's local access
  as tight as its SSH access and never place a same-host reverse proxy in front of
  the admin.

- **No Tailscale.** SSH is the only inbound port. This works with a corporate VPN
  up — the tunnel rides your normal SSH access.
- **Loop notifications / questions:** this workflow provisions **no** Slack
  credential, so notifications are not wired out of the box. To enable them, set
  the overlay's Slack bot token on the box and re-deploy (`t3 setup slack-bot`, or
  add the Slack config to `teatree.env`); teatree then talks to Slack over Socket
  Mode (outbound only — no inbound webhook, nothing extra to expose). Until then,
  follow the loop through the admin dashboard and `docker compose logs`.
- **Autostart on reboot:** the compose `restart: unless-stopped` policy plus Docker
  enabled on boot bring the worker and admin back after a reboot.
- **Updates:** teatree self-updates in-loop via `t3 update` (deferred reinstall on
  the editable clone), and the entrypoint fast-forwards the runtime clone from
  origin on every (online) boot — the baked snapshot is only the *first-boot* /
  offline floor, never a replacement for in-loop self-update. There is **no**
  second workflow and no quiescence probe — a re-run of the deploy workflow is only
  needed for infra/compose/image changes.

## Running the `t3` CLI on the box (#3232)

teatree runs **exclusively in Docker** — the CLI as well as the servers — so the
box needs no host Python / uv / py3.13 / prek / direnv / ttyd. `deploy/t3` is a
container-wrapping entry: it `docker compose exec`s into the running
`teatree-worker` (falling back to a one-off `run --rm` when the stack is down) so
`t3 <args>` executes inside a container that shares the live DB, credential, and
session mounts.

`t3 setup` (run by the `init` role, and available to run on the host) installs a
shell alias into `~/.bashrc` (and `~/.zshrc` when present) so `t3 …` on the host
transparently invokes the containerized CLI:

```bash
# What the alias resolves to (installed automatically by `t3 setup`):
alias t3="/home/teatree/teatree-deploy/deploy/t3"
```

`t3 doctor` verifies the wiring once you have opted in (compose stack present, the
`deploy/t3` entry executable, `docker` on PATH, and the alias not pointing at a
stale clone path) and WARNs with the fix — re-run `t3 setup` — when a piece is
missing. The alias install and the doctor check both no-op **inside** a container
(there the container *is* the CLI). Override the target service with
`TEATREE_DOCKER_CLI_SERVICE` if needed.

## Configuring the loop agent — `~/.claude/settings.json` (#3359)

The containerized agent's `~/.claude/settings.json` is **generated by the `init`
role** from the image-baked, committed template `deploy/claude-settings.template.json`
(plus env overrides) *before* `t3 setup` runs — so the loop's agent has a
deliberate model, permission mode, `autoMode` grants, `autoCompactEnabled`, and a
box-sized `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` instead of stock CLI defaults.
Only `~/.claude/projects` is bind-mounted from the host; the reason that mount is
scoped is **credentials** (`~/.claude/.credentials.json`), which stay host-only —
settings are non-secret and get their own provisioning path here.

Configure the loop agent by editing the committed, reviewable
`deploy/claude-settings.template.json` and re-deploying, or override the box-specific knobs
via `teatree.env`:

| Env var | Overrides |
| --- | --- |
| `TEATREE_CLAUDE_MODEL` | `model` |
| `TEATREE_CLAUDE_PERMISSION_MODE` | `permissions.defaultMode` |
| `TEATREE_CLAUDE_TOOL_CONCURRENCY` | `env.CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` |

The seed **deep-merges** so a redeploy re-asserts the deploy-managed keys while
preserving keys `t3 setup` adds afterwards (notably `statusLine`). Editing the
host operator's own `~/.claude/settings.json` does **not** configure the loop —
the loop reads the container's generated file, not the host's.

## Self-heal watchdog (H24, owner directive #10)

The factory once froze for ~7 hours and nobody noticed because **the worker was
the monitor** — when the init/worker died, the thing that would have alerted died
with it. The fix is a watchdog that lives **outside** the thing it watches, kept
alive by a supervisor that survives a full stack outage. The **Docker daemon** is
the only supervisor present on both Linux and macOS, so the watchdog is an
**in-daemon sidecar container** (`teatree-watchdog`), not a host-level OS timer.
This supersedes the Linux-only systemd timer of #3289 with a single mechanism
that works identically on a Linux box and a macOS Docker Desktop host.

The watchdog is just another compose service — **the deploy installs it; there is
nothing to enable.** It has `restart: always` and **no** `depends_on`, so the
daemon (re)starts it even when `teatree-init` is crash-looping and every app
service is down — exactly the outage it exists to repair.

`deploy/watchdog.sh --loop` runs a pass every `TEATREE_WATCHDOG_INTERVAL` seconds
(default 300) and, each pass:

1. `docker compose -p teatree up -d --no-recreate` — restarts anything that went
   down (a full stack outage, like the init crash-loop, is auto-repaired here).
   This is the **only** mutating docker op — the watchdog never prunes, stops,
   recreates, or removes anything, so it is safe to run unattended. The pass is
   **gated on init state**: a completed one-shot init (`exited 0`) is *excluded*
   from the `up -d`, because `up -d --no-recreate` re-runs a completed one-shot
   init on every pass (verified empirically) and that would replay the heavy
   ~minute init every 5 minutes; a *missing or failed* init is included, so the
   init-failure outage still recovers.
2. `t3 doctor check --json` inside a live container — reads the factory health,
   including the H24 self-heal detectors: a compose init container that exited
   non-zero / a worker stuck `Created`, a free worker flock over overdue loop
   work, an `execute_headless_task` stranded RUNNING with no live worker, a READY
   loop timer stale past 2× its cadence, a PENDING `interactive` task under
   `agent_runtime=headless`, a FAILED task on a still-live ticket, and a runtime
   clone drifted off its default branch.
3. On any **red** finding it DMs the owner via `t3 teatree notify send`, keyed on
   the finding set so an ongoing outage does not re-spam every pass. (The default
   deploy wires no Slack credential; until you add one the DM step no-ops and the
   findings are visible in the watchdog's own container logs and `t3 doctor check`.)

The DM leaves the box via a `docker compose exec` inside a *live app container*,
not from the watchdog itself — the watchdog runs `network_mode: none`, so the
docker socket is its only channel.

### What it does — and does not — survive

Being a container the daemon supervises, the watchdog covers the outages that a
same-daemon supervisor can cover, and is honest about the two it cannot:

| Failure | Recovered? | How |
| --- | --- | --- |
| `teatree-init` crash-loop (the recorded 7h freeze) | ✅ | next pass's gated `up -d --no-recreate` re-runs the failed init; while the root cause persists the doctor step reddens and DMs the owner |
| an app service crashed / exited | ✅ | `up -d --no-recreate` restarts it |
| the **watchdog itself** crashed | ✅ | `restart: always` — the daemon relaunches it in seconds |
| the daemon restarted (e.g. host reboot with Docker enabled on boot) | ✅ | `restart: always` brings it back with the stack |
| `docker compose down` (deliberate teardown) | ❌ (intentional) | the operator took the stack down on purpose; nothing should fight that |
| the Docker **daemon** is dead | ❌ | its supervisor is gone; an external uptime check is the backstop |
| the **host** is dead / unreachable | ❌ | out of scope for any in-host mechanism; use an external ping |

### Observe it

The watchdog is a normal compose service, so its passes are in its container logs:

```bash
docker compose -p teatree logs -f teatree-watchdog
```

### Knobs (compose `environment:` on the `teatree-watchdog` service)

| Env var | Default | Purpose |
| --- | --- | --- |
| `TEATREE_WATCHDOG_INTERVAL` | `300` | seconds between passes in `--loop` mode |
| `TEATREE_WATCHDOG_PASS_TIMEOUT` | `300` | hard cap on a single pass; a wedged pass is killed and the loop continues |
| `TEATREE_WATCHDOG_PROJECT` | `teatree` | the compose project the watchdog drives |
| `TEATREE_WATCHDOG_OVERLAY` | `teatree` | the overlay used for the owner DM |
| `TEATREE_WATCHDOG_EXEC_SERVICES` | `teatree-admin teatree-worker` | services (first reachable wins) to run the doctor/DM commands in |
| `TEATREE_WATCHDOG_APP_SERVICES` | `teatree-worker teatree-admin teatree-slack-listener teatree-watchdog` | services restarted when init has already completed (init excluded) |
| `TEATREE_WATCHDOG_INIT_SERVICE` | `teatree-init` | the one-shot init service the pass gates on |

It needs `python3` in the image for the richest DM body (baked into the image);
without it the DM degrades to a generic "red findings" body.

### macOS (Docker Desktop) notes

The same compose service runs unchanged on a macOS Docker Desktop host. Two
Docker Desktop settings matter:

- **Docker socket:** enable *Settings → Advanced → "Allow the default Docker
  socket to be used"* so `/var/run/docker.sock` is present for the socket mount.
- **File sharing:** the read-only checkout bind mount (`/home/teatree/teatree-deploy`)
  must be under a path Docker Desktop is allowed to share (*Settings → Resources →
  File sharing*). Adjust the bind source in `deploy/docker-compose.yml` if your
  checkout lives elsewhere on the Mac.
- **Start at login:** enable *Settings → General → "Start Docker Desktop when you
  sign in"* so the daemon — and therefore the watchdog — comes back after a
  reboot, the macOS analogue of "Docker enabled on boot" on Linux.

### Decommissioning the old systemd timer (#3289)

If a box previously installed the Linux-only systemd watchdog, remove it (the
in-daemon watchdog replaces it):

```bash
sudo systemctl disable --now teatree-watchdog.timer && sudo rm -f /etc/systemd/system/teatree-watchdog.{service,timer}
```

## Caveats

- **Headless orchestration is still maturing** — expect to babysit early runs via
  the admin dashboard and `docker compose logs` (and Slack once you wire it up).
- **The admin runs under gunicorn** (a production WSGI server) bound to the box
  loopback behind an SSH tunnel — never expose it publicly, and never place a
  same-host reverse proxy in front of it: the auto-login trust model treats any
  local `127.0.0.1` client (a proxy included) as superuser. Both long-running
  services (worker AND admin) run with `DEBUG` off to avoid `connection.queries`
  growth; `/admin/` mounts independent of `DEBUG`, so nothing here relies on it.
- **The OAuth token shares your weekly Claude quota and does not auto-refresh** —
  rotate `CLAUDE_CODE_OAUTH_TOKEN` manually and re-run the workflow when it expires.
- **Config seeds carry provenance and never freeze a changed default.** The init
  role seeds DB config via the provenance-aware `t3 teatree config_setting seed`
  (`deploy/entrypoint.sh`): it never writes a value equal to the code default
  (which would only freeze a future default change), preserves any operator
  override, and re-seeds a row it still owns when the shipped default changes.
  A plain `t3 doctor` never mutates the DB; `t3 doctor check --repair` may clear
  a stale **entrypoint-seeded** `provision_max_concurrency` pin (never an
  operator's deliberate one).
- **teatree-only:** this deployment never clones or references any customer or
  product overlay — only the built-in `t3-teatree`.
