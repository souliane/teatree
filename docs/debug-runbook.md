# Debug-access runbook (#3162)

Three tiers of debug access to a running teatree box, weakest coupling first. The
dashboard owns tiers 2 and 3; **tier 1 is the break-glass and is deliberately NOT
served by teatree** — a debug path served by the patient cannot treat the patient.

## Tier 1 — break-glass: host SSH + tmux (build nothing, document only)

When teatree itself is broken — Django won't boot, the DB is locked, gunicorn is
dead — anything teatree *serves* is broken with it, including the dashboard's own
web terminal. The primary debug path must therefore have a failure domain
independent of teatree: the host's own sshd (already how the one-click deploy
connects and how the loopback admin is reached over the tunnel).

```bash
# From your laptop — reach the box's host shell (key-only auth, host sshd):
ssh <box>

# Attach a durable session so work survives a dropped connection:
tmux new -A -s teatree

# Drop into the teatree checkout and drive it by hand:
cd <teatree-checkout>
t3 doctor check
claude            # a fresh interactive session in the checkout
```

teatree adds **no** sshd of its own — the host already provides one with key-only
auth; duplicating it inside the app would only add a weaker boundary. Reach the
loopback admin/dashboard the same way it is always reached:

```bash
ssh -L 8000:127.0.0.1:8000 <box>   # then open http://127.0.0.1:8000/dash/ locally
```

## Tier 2 — convenience: the dashboard's loopback ttyd "Debug session" button

When teatree is *up* but a ticket is stuck, the ticket drawer's **Debug session**
button spawns `ttyd --writable --once` on a free `127.0.0.1` port wrapping a
`claude` session (fresh, or `--resume <session>` from the card). It binds loopback
only, is reached through the **same** SSH tunnel (`ssh -L <port>:127.0.0.1:<port>`),
and `--once` makes the process die with the session — no lingering writable
terminal, no new network door. If the Django process is down the button does not
render; fall back to tier 1.

## Tier 3 — phone-friendly: allowlisted command buttons

The health page carries a fixed, code-defined allowlist of read-only-ish `t3`
verbs (`t3 doctor check`, `t3 worker status`, `t3 loops list`, `t3 loops tick
--loop <name>`) run as bounded subprocesses (timeout + captured output, audited).
No free-form shell, no operator-supplied argv — enough to poke a stuck factory
from a phone over the tunnel without a terminal.

## What is deliberately NOT built

- **No sshd inside teatree** — the host already has one; a `--writable` terminal
  must sit behind the strongest boundary available (SSH), not a Django session.
- **No custom websocket→PTY bridge** — that rebuilds ttyd with a fresh attack
  surface inside the Django process. ttyd behind loopback + the SSH tunnel is the
  boundary.
- **No exposure beyond loopback** — every tier stays on `127.0.0.1` reached only
  through the SSH tunnel.
