# `t3 browser` — session mechanics, steps and the owner-facing Playwright tools

The mechanics behind `/t3:e2e` § "Browser tool: `t3 browser` (Playwright, headless)". That section carries the headless rule and what the tool is for; this file carries how a session works, every step, the output, and how the browser is provisioned.

## One browser per worktree

- `t3 browser open` starts a keeper process that launches a headless Chromium with a DevTools endpoint. Every later step re-attaches to that browser over CDP instead of launching a new one, so cookies, the page and its state carry across steps.
- The keeper records every console message, page error, failed request, response and navigation to the session's event log, so `inspect` reports what an earlier `open` or `act` caused.
- A session belongs to the worktree you run `t3` from (the git toplevel of the working directory). Its files live under `<teatree data dir>/browser-sessions/<slug>/`, readable by your user only: `events.jsonl`, `keeper.log`, and `inspect/` (`aria.yaml`, `page.html`, `page.png`).
- It ends on `t3 browser close`, by itself after 30 idle minutes, or when teatree tears the worktree down (removing that directory stops the keeper).
- Exit `0` means the step ran — the findings are in the output. Exit `1` means it could not run (no browser, no session, a Playwright error, a page that did not load in time) and says why, after printing what the page did up to that point; it never prints an empty result instead.

## Steps

| Command | Does |
| --- | --- |
| `t3 browser open <url> [--timeout <s>]` | Launch or reuse the browser, load the page (waiting up to 30 s for its load event unless `--timeout` says otherwise), wait for it to go quiet, print what it did. |
| `t3 browser act <verb> <args>` | One interaction on the open page (verbs below), then what it caused. Needs an open session. |
| `t3 browser inspect` | Save the accessibility snapshot, HTML and screenshot; print the first 200 snapshot lines and every finding since the page loaded. Needs an open session. |
| `t3 browser close` | End the session (a no-op when there is none). |

`act` verbs take a Playwright selector (`text=Go`, `role=button[name="Save"]`, a CSS selector) as their first argument:

| Verb | Arguments | Does |
| --- | --- | --- |
| `click` | `SELECTOR` | Click the element. |
| `fill` | `SELECTOR VALUE` | Replace the field's value. |
| `type` | `SELECTOR TEXT` | Type key by key, firing key events. |
| `press` | `SELECTOR KEY` | Press one key (`Enter`, `Control+A`) on the element. |
| `upload` | `SELECTOR FILE...` | Set the file input's files. |
| `wait` | `SELECTOR` | Wait until the element is visible. |
| `eval` | `EXPRESSION` | Evaluate JavaScript in the page and print its result. |

## Output

Each step prints `URL` and `TITLE`, then one line per console message and per finding:

```text
CONSOLE error diag-boom
PAGEERROR TypeError: x is undefined
FAILED GET http://127.0.0.1:8000/api/items — net::ERR_CONNECTION_REFUSED
HTTP 404 http://127.0.0.1:8000/static/logo.png
```

`--json` prints one object per step instead, carrying every recorded event (successful responses and navigations included); `inspect --json` adds the snapshot text and the three file paths.

## Provisioning

Playwright is a runtime dependency of teatree, and the worker image installs the locked release's `chromium-headless-shell`. `t3 doctor check` FAILs when that browser cannot launch; `t3 doctor check --repair` runs `python -m playwright install chromium-headless-shell` and probes again. Performance traces, CPU/network throttling and heap snapshots are not exposed; they are reachable through Playwright's `context.new_cdp_session(page)` when a skill needs them.

## Owner-facing Playwright tools

These are for a human at the keyboard; an agent never opens them.

Both servers below run code or show test data to whoever reaches the port, so they listen on loopback only and you reach them through an SSH port forward from your own machine: `ssh -N -L <port>:127.0.0.1:<port> <box>`, then open `http://127.0.0.1:<port>` locally.

- **UI mode** in the E2E clone: add `--ui-host 127.0.0.1 --ui-port <port>` to the clone's Playwright test command.
- **Trace viewer**: `python -m playwright show-trace --host 127.0.0.1 --port <port> <trace.zip>`.
- **`PWDEBUG=1`, `page.pause()` and `playwright codegen`** need a display, so they only work on a machine that has one.
