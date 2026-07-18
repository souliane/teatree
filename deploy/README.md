# teatree headless deployment (Hetzner)

Run teatree headless on a single existing Hetzner ARM64 box (CAX21, 8 GB), driven
by one manual GitHub Action. teatree runs the autonomous loop (`t3 worker`,
`agent_runtime=headless`), self-updates via its own loop, autostarts on reboot,
and serves the Django admin on the box loopback for SSH-tunnel access.

This is **teatree-only** — no customer or product overlays. The only registered
overlay is the built-in `t3-teatree`.

## How the one-click deploy works

`Actions → Deploy teatree to Hetzner → Run workflow` (`.github/workflows/deploy-hetzner.yml`):

1. Installs a checksum-pinned `hcloud` CLI and resolves the box IP from the server
   name (the IP is masked out of the logs immediately).
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
| `teatree-worker` | `t3 worker` | `unless-stopped` | the loop cadence owner; `DEBUG` off |
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
| `HCLOUD_TOKEN` | Hetzner Cloud API token (read-only is enough) to resolve the box IP |
| `HETZNER_SERVER_NAME` | the Cloud server name to resolve |
| `HETZNER_SSH_USER` | the deploy user (e.g. `teatree`) |
| `HETZNER_SSH_PORT` | the SSH port |
| `HETZNER_SSH_KEY` | the deploy **private** SSH key |
| `HETZNER_SSH_KNOWN_HOSTS` | the box's host key line(s) for strict checking |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token — the headless auth |
| `TEATREE_GH_TOKEN` | GitHub token for the loop. Needs **write** on `issues`, `pull_requests`, and `contents` (plus `metadata: read`) — `init` preflights these and fails loud if any is missing (#3405) |
| `T3_ADMIN_USER` | Django admin superuser name |
| `T3_ADMIN_PASSWORD` | Django admin superuser password |
| `GIT_AUTHOR_NAME` | git identity for the loop's commits |
| `GIT_AUTHOR_EMAIL` | git identity email (use a GitHub noreply for public repos) |

The `known_hosts` line comes from `ssh-keyscan -p <port> <box-host>` (run once,
verify the fingerprint out of band).

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
  the editable clone). There is **no** second workflow and no quiescence probe — a
  re-run of the deploy workflow is only needed for infra/compose/image changes.

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
- **teatree-only:** this deployment never clones or references any customer or
  product overlay — only the built-in `t3-teatree`.
