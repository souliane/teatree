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

The stack is three services from one image (`deploy/Dockerfile`), selected by
`TEATREE_ROLE`:

| Service | Role | Restart | Notes |
| --- | --- | --- | --- |
| `teatree-init` | one-shot prep | `no` | clone + editable install + `t3 setup` + DB config; exits 0 |
| `teatree-worker` | `t3 worker` | `unless-stopped` | the loop cadence owner; `DEBUG` off |
| `teatree-admin` | `t3 admin` | `unless-stopped` | Django admin under gunicorn on the box loopback `127.0.0.1:8000` (host networking); `DEBUG` off |

`worker` and `admin` wait for `init` to complete, so the editable install on the
shared clone happens exactly once. All three share named volumes, so the admin and
the worker read the **same** `db.sqlite3` (WAL — safe concurrent reads):

| Volume | Mount | Holds |
| --- | --- | --- |
| `teatree_src` | `/home/teatree/teatree` | the teatree clone (source) |
| `teatree_data` | `/home/teatree/.local/share/teatree` | the canonical DB (`db.sqlite3`) |
| `teatree_worktrees` | `/home/teatree/.local/share/teatree-worktrees` | per-worktree isolated DBs |
| `teatree_workspaces` | `/home/teatree/workspace/t3-workspaces` | ticket worktrees |
| `teatree_uv` | `/opt/teatree/uv` | the runtime teatree Python + venv + `t3` shims |

Re-running the workflow is **idempotent** — it converges the same stack.

## Fleet role split — which loops run where

teatree runs as a small fleet: the operator's laptop plus this headless box.
Some autonomous loops are **fleet-scoped** — they must run on exactly **one**
instance or they double-act. The box provisions **no Slack credential** (see
[Access & networking](#access--networking)), so the Slack-facing loops would be
both broken here and duplicates of the laptop's. The split:

| Instance | Runs | Does not run |
| --- | --- | --- |
| **Laptop** | `inbox` (Slack drain), `review` (colleague PR review → Slack), `directive_loop` (asks the human via Slack) | `tickets` — the operator disables it by hand |
| **Box** (this deploy) | `tickets` (issue scanning/dispatch) + all machine-local loops | `inbox`, `review`, `directive_loop` |

The box enforces its side in `deploy/entrypoint.sh` (init role): after seeding
config it calls `t3 loop disable <name>` — the single DB-backed per-loop control
plane (`Loop.enabled` AND `LoopsConfig.is_enabled`) — for each fleet-scoped loop.
The disable is idempotent, so re-running the deploy converges.

`TEATREE_DISABLED_LOOPS` (comma-separated) overrides the set the box disables. It
defaults to `inbox,review,directive_loop` when unset, so a fresh deploy is safe
with no configuration; an **empty** value disables nothing (every loop runs
here). Set it in the box env file (`teatree.env`) to change the set. Because the
deploy workflow rewrites `teatree.env` from repository secrets on every run, a
persistent override belongs in that workflow's env-file writer (add a
`TEATREE_DISABLED_LOOPS` line beside the others), not a hand-edit on the box.

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
| `TEATREE_GH_TOKEN` | GitHub token for the loop (clone/read/PRs) |
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
