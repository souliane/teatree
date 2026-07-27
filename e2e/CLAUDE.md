# e2e — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `e2e/`.

- **Load `/t3:e2e` before working here.** It covers Playwright test writing, running, visual snapshots, evidence posting, and the pre-push visual QA gate.
- **Full worktree per PR (non-negotiable).** Each PR under test gets its own backend + frontend via `t3 <overlay> worktree provision` + `t3 <overlay> worktree start`. Never mix one worktree's backend with another's frontend; never hand-patch an incomplete worktree — delete and recreate.
- **Evidence comes from the deployed environment**, never from local builds or `localhost` screenshots (`/t3:rules` § "Evidence Comes From the Deployed Environment").
- **Never blindly accept snapshot baselines** — verify the captured PNG shows the asserted state before updating (`/t3:e2e` visual QA gate).
- **On a host Playwright has no browser build for, name an installed chromium** via `E2E_CHROMIUM_EXECUTABLE`. `playwright install` refuses on an unrecognised platform — and *exits 0 while printing `Failed to install browsers`*, so never gate provisioning on its exit code. CI leaves the variable unset and launches Playwright's own browser:

  ```bash
  E2E_CHROMIUM_EXECUTABLE=/snap/chromium/current/usr/lib/chromium-browser/chrome \
    uv run pytest e2e/dash --ds=e2e.dash.settings
  ```

  Two specs depend on the host rather than the browser: `test_terminal_button_click_renders_launch_url` needs `ttyd` on `PATH`, and `test_admin_index_loads_clean` fails against a full chromium because it requests `/favicon.ico` (404) while CI's `chrome-headless-shell` never does.
