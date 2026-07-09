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
| `teatree-admin` | `t3 admin` | `unless-stopped` | Django admin on `127.0.0.1:8000`; `DEBUG` on (admin only) |

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

2. **Install Docker Engine + the compose plugin** and enable it on boot:

   ```bash
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

- **Admin (Django):** the admin binds to the box loopback only. Reach it through an
  SSH tunnel — no port is exposed publicly:

  ```bash
  ssh -L 8000:localhost:8000 -p <ssh-port> <ssh-user>@<box-host>
  # then open http://localhost:8000/admin
  ```

  There is **no admin login prompt**: while DEBUG is on (the admin service), the
  auto-login middleware authenticates the first superuser on every `/admin/`
  request, so the **SSH tunnel to the loopback — not an admin password — is the
  security boundary**. `T3_ADMIN_USER` / `T3_ADMIN_PASSWORD` still seed that
  superuser row deterministically (they only matter as a password in a DEBUG-off
  context).

- **No Tailscale.** SSH is the only inbound port. This works with a corporate VPN
  up — the tunnel rides your normal SSH access.
- **Loop notifications / questions** go out over Slack (Socket Mode, outbound only)
  — no inbound webhook, nothing extra to expose.
- **Autostart on reboot:** the compose `restart: unless-stopped` policy plus Docker
  enabled on boot bring the worker and admin back after a reboot.
- **Updates:** teatree self-updates in-loop via `t3 update` (deferred reinstall on
  the editable clone). There is **no** second workflow and no quiescence probe — a
  re-run of the deploy workflow is only needed for infra/compose/image changes.

## Caveats

- **Headless orchestration is still maturing** — expect to babysit early runs via
  the admin and Slack.
- **The admin is a Django dev server** behind a loopback + SSH tunnel. Never expose
  it publicly; `DEBUG` is on for the admin service only (the worker runs with
  `DEBUG` off to avoid `connection.queries` growth in a long-running process).
- **The OAuth token shares your weekly Claude quota and does not auto-refresh** —
  rotate `CLAUDE_CODE_OAUTH_TOKEN` manually and re-run the workflow when it expires.
- **teatree-only:** this deployment never clones or references any customer or
  product overlay — only the built-in `t3-teatree`.
