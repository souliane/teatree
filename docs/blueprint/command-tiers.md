# BLUEPRINT Appendix — Command Tiers

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §8 (Management commands, global CLI, overlay subcommands, overlay contract-check, dev loop, teatree source resolution).

## 8. Command Tiers

| Tier | Tool | Needs Django? | Examples |
|------|------|---------------|----------|
| Runtime commands | django-typer management commands | Yes | `worktree provision`, `tasks work-next-sdk`, `followup refresh` |
| Bootstrap commands | Typer CLI (`t3`) | No | `t3 startoverlay`, `t3 info`, `t3 ci cancel` |
| Overlay commands | Typer CLI delegating to manage.py | Via subprocess | `t3 acme start-ticket`, `t3 acme worktree start` |

Internal utilities (`utils/`) — port allocation, git helpers, DB ops — are Python modules, not CLI-facing commands. They underpin all three tiers but are not a tier themselves.

### 8.1 Management Commands (django-typer)

**lifecycle** — Worktree provisioning:

- `setup(ticket_id, repo_path, branch)` → creates Worktree, calls `provision()`, runs overlay provision_steps
- `start(worktree_id)` → calls `start_services()`
- `status(worktree_id)` → returns state dict
- `teardown(worktree_id)` → calls `teardown()`
- `clean(worktree_id)` → full teardown + state cleanup
- `diagram(model="worktree"|"ticket"|"task")` → Mermaid state diagram from FSM transitions

**tasks** — Task routing and execution:

- `create(ticket, phase, reason | reason-file, interactive=False)` → enqueues the next-phase task (used by `/t3:next` for phase handoff; headless by default so a worker claims immediately)
- `claim(execution_target, claimed_by, lease_seconds=120)` → claims next pending task
- `work-next-sdk(claimed_by)` → executes headless task via `claude -p`
- `start(task_id?, claimed_by)` → claims an interactive task and execs `claude` in the current terminal

**followup** — GitLab sync:

- `refresh()` → counts pending tasks and tickets
- `remind(channel)` → sends reminders
- `sync()` → calls `sync_followup()` to create/update tickets from PRs
- `discover-prs()` → discover open PRs awaiting review

**workspace** — Workspace operations (ticket setup, status, finalize, cleanup)
**worktree** — Per-worktree commands (`provision`, `start`, `verify`, `teardown`, `clean`, `ready`, `status`)
**db** — Database operations (`refresh`, `restore-ci`, `reset-passwords`, `query`, `shell`). `query "<sql>"` runs a read-only SQL statement against the live Django connection (the *same* control DB the gate reads — `teatree.paths.CANONICAL_DB` decides canonical-vs-worktree-isolated once at settings-load, so introspection cannot drift from `pr create` / `lifecycle visit-phase`) and emits rows as JSON; read-only is enforced in two layers — a best-effort leading-keyword pre-filter (`select`/`pragma`/`explain` only, PRAGMA setters rejected) plus a binding enforced read-only transaction (Postgres `SET TRANSACTION READ ONLY`, SQLite `PRAGMA query_only=ON`). `shell` delegates to Django's own shell against that same connection (#774)
**env** — Read/write the env cache (`set`, `show`, `check`, `migrate-secrets`). `migrate-secrets` moves any `POSTGRES_PASSWORD=<literal>` line out of `.t3-env.cache` into the local `pass` store under `teatree/wt/<ticket_id>/postgres` so the on-disk cache only carries the symbolic `POSTGRES_PASSWORD_PASS_KEY` reference.
**run** — Service runner (uses `lifecycle.compose_project()` shared helper)
**pr** — PR creation and validation
**ticket** — Ticket transitions and queries (`list`, `transition`, `sync-completions`, `comment`). `comment <issue_url> --body/--body-file` posts a tracker comment via the per-URL `CodeHostBackend` (GitLab issues + work items, GitHub issues).
**tool** — Overlay-declared tool subcommands (declared via `OverlayBase.get_tool_commands()`)
**e2e** — `e2e run|external|project` (Playwright dispatcher)
**overlay** — Overlay inspection (`config`, `info`)
**generate_all_docs / generate_overlay_docs / generate_skill_docs** — internal client-term-redacted entry points (called from pre-commit hooks)

### 8.2 Global CLI Commands (`t3`)

Typer-based, work without Django:

- `t3 startoverlay` — scaffold a new overlay package (see §6.3)
- `t3 agent` — launch Claude Code with teatree context (for developing teatree itself)
- `t3 info` — show entry point, sources, editable status, discovered overlays (with project paths), Claude plugin install, and agent runtime skill dirs
- `t3 sessions` — list/resume Claude conversation sessions
- `t3 docs` — serve mkdocs documentation (requires `docs` dependency group)
- `t3 ci {cancel,divergence,fetch-errors,fetch-failed-tests,trigger-e2e,quality-check}` — CI helpers
- `t3 <overlay> e2e run [<work-item>] [<test-path>] [--at last-green|main] [--target dev|local]` — run E2E tests; dispatches to the project runner (in-repo pytest-playwright) or the external runner (remote Playwright repo) based on the overlay's `get_e2e_config()` — same command across overlays. With a `<work-item>` (a Ticket pk / issue number / issue URL — the #794 keystone) it resolves the work item by its Ticket natural key, applies the **default environment ladder** (existing workspace on disk → recorded last-green SHA-set → `origin/main`; `--at last-green|main` overrides), runs, and records `{result, timestamp, per_repo_shas}` to the DB-durable recipe at `Ticket.extra['e2e_recipe']` so a rerun never re-discovers prerequisites serially. Reconcile-on-read drops a `Worktree` row whose recorded path is gone (never "DB says X, disk says Y, run anyway"). Deterministic outcome: the e2e result, or a precise readiness failure naming the exact provisioning gap (which repo at which ref). On green the run's SHA-set becomes the new last-green baseline; a failed run records provenance but never moves the baseline. See `teatree.core.e2e_workitem`
- `t3 <overlay> e2e external [--repo <name>] [--target dev|local] [<test-path>]` — explicit external runner: Playwright from `T3_PRIVATE_TESTS` or a named `[e2e_repos.<name>]` git repo; skips port discovery when `BASE_URL` is already set (DEV/staging mode)
- `t3 <overlay> e2e project [<test-path>] [--target dev|local] [--update-snapshots]` — explicit project runner: pytest-playwright in the overlay's own test dir, executed in the canonical Docker image by default
- `--target dev|local` — dual-env selector (omitted = back-compat inference from `BASE_URL`). Exported as `T3_E2E_TARGET`; a dual-mode spec branches on it rather than a `BASE_URL` host regex. `local` always discovers the local frontend so it can never silently hit a deployed env
- `t3 review {post-draft-note,post-comment,delete-draft-note,delete-discussion,list-draft-notes,publish-draft-notes,update-note,reply-to-discussion,resolve-discussion,approve,unapprove}` — code-host draft notes (post/delete/list/publish), in-place edits of draft or published notes, removal of *published* discussions (`delete-discussion`, gated — distinct from `delete-draft-note` which is pre-publication and ungated), immediate replies on existing discussion threads and resolve/unresolve toggle, plus the GIVE-review `approve` / `unapprove` actions. `approve` enforces a review-first precondition (refuses unless a review note authored by the approving identity already exists on the MR), and both approve/unapprove respect the `on_behalf_post_mode` pre-gate (souliane/teatree#960, BLOCK under `ask` / `draft_or_ask`). Every body-bearing publish method (`post-comment`, `post-draft-note`, `reply-to-discussion`, `update-note`) additionally routes through the colleague-MR review-shape gate (`teatree.cli.review_shape_gate`, souliane/teatree#1114): on a colleague MR (MR author != current identity), MR-level prose is capped at 2 sentences / 280 chars and inline notes at 4 sentences; a longer body is refused with steering text before the GitLab API call. Own-MR posts are exempt.
- `t3 review-request discover` — discover open PRs awaiting review (each MR carries a live-verified `review_already_requested` / `review_permalink`, read with the post-token, #1084)
- `t3 review-request check --mr-url <url>` — race-safe pre-post dedup gate (#1084): reads the live review channel with the *same* token an outbound post would use (Connect ⇒ user `xoxp`, read-token == post-token), recency-bounded (24h) and fail-safe, then takes the atomic `ReviewRequestPost` claim. Prints `{"action": "post"|"suppress", "permalink", "author", "reason"}`. The agent runs it in the same turn as the post (SKILL.md mandate) and aborts on `suppress`; the loop's `ReviewNagScanner` consults it before nagging so an out-of-band/user post stops the nag train. Reconciles `ReviewRequestPost.done_at` + `PullRequest` OPEN→REVIEW_REQUESTED only — it never touches the loop `Task` lifecycle (owned by #1086/#1074/#1077). Reuses `ReviewRequestPost`; no migration.
- `t3 review-request post --mr-url <url> --approver <id> [--title <t>]` — the **sanctioned authorized-post** half of #1084/#1094 (#1098). One classifier-legible transaction: the #1084 `review_request_check` live-channel dedup + atomic claim, then the #960 `require_on_behalf_approval` chokepoint (no recorded, unconsumed, exactly-scoped `OnBehalfApproval` ⇒ refuse with the exact `t3 review approve-on-behalf '<url>' review_request_post --approver <id>` remediation, exit 2), then the post to `get_review_channel()`, consuming the approval + writing the `OnBehalfAudit`. Prints `{"action": "post"|"suppress"|"refused", ...}`. On refusal it rolls back the guard's just-created `ReviewRequestPost` claim (else every future legitimate post would suppress `already_claimed` forever). `canonical_mr_url` is used for both the guard arg and the approval target so they are provably one string. The permalink is recorded to `<T3_DATA_DIR>/tickets/<iid>/mr_review_messages.json` as a durable record (never a dedup oracle — dedup stays the live guard). The only sanctioned post path; raw `messaging_from_overlay(...).post_message(...)` is forbidden (`/t3:review-request` § 7). Reuses `ReviewRequestPost`/`OnBehalfApproval`/`OnBehalfAudit`; no migration.
- `t3 tool {privacy-scan,analyze-video,bump-deps,label-issues,find-duplicates,triage-issues,audit-memory}` — standalone utilities
- `t3 config write-skill-cache` — write overlay skill metadata to cache
- `t3 doctor {check,repair}` — health checks and symlink repair
- `t3 doctor authorizations` — read-only: detect which generic recommended auto-mode authorizations are absent from the user's resolved `~/.claude/settings.json` `autoMode.allow` and print the paste-ready sentence for each missing one. Teatree ships **no** classifier whitelist of its own (§11.4 — classifier rules always remain per-user); this only *suggests*, never writes the user's settings. The recommended set + render logic lives in `teatree.cli.recommended_authorizations`; it is also surfaced by `t3 doctor check` and at the end of `t3 setup`. User-specific items (VPS hosts, dev-DB creds, exact paths) are deliberately not part of the generic set.
- `t3 update` — fetch + fast-forward (ff-only) teatree core and every registered overlay repo to its default branch, reinstall advanced editable installs, then re-run the idempotent `t3 setup`. A dirty tree, a non-default-branch checkout, or a missing upstream is skipped with a reason (never stashed/reset/clobbered); exit is non-zero only on a hard fetch/pull failure, not a skip. Kept separate from `t3 setup` so routine bootstrap can never silently jump the running code to newer `main`.
- `t3 setup slack-bot --overlay <name>` — interactive walkthrough to register a Slack bot for an overlay; opens the app-manifest URL, captures bot+app tokens, stores them via `pass`, writes `slack_user_id` into `~/.teatree.toml`, smoke-tests with a round-trip DM (see § 10.1 for the manifest template and scopes). Subcommands of `t3 setup` short-circuit the global skill-install callback so the walkthrough runs without requiring `T3_REPO`.
- `t3 assess` — codebase health check (ruff, coverage, complexity, dependency staleness)
- `t3 infra` — infrastructure helpers (e.g. shared docker container management)
- `t3 loop {start,stop,status,tick}` — manage the long-lived `/loop`. `start` registers the loop in the active Claude Code session; `tick` runs one tick out-of-band (used by tests and by manual investigation). `--overlay <name>` restricts a tick to a single overlay (default: scan every registered overlay).
- `t3 loop slack-answer {run,status,start}` — the third `/loop` slot (§ 5.8): the reactive, token-cheap Slack-answer loop. `start` prints the `/loop <cadence>` slot line to paste in the loop-owner session; `run` runs one bounded reactive cycle (`loop_slack_answer`); `status` shows the un-answered queue depth. Cadence default 20s, env `T3_SLACK_ANSWER_CADENCE` (floor 15s).

### 8.3 Overlay Commands (`t3 <overlay> ...`)

Each registered overlay gets a subcommand group (e.g., `t3 acme`). Commands delegate to `manage.py` via subprocess — the overlay's Django settings are used automatically.

**Shortcuts:**

- `t3 <overlay> start-ticket <URL>` — create ticket, provision, start services
- `t3 <overlay> ship <ID>` — create PR for a ticket
- `t3 <overlay> daily` — sync PRs, check gates, remind reviewers
- `t3 <overlay> full-status` — ticket/worktree/session summary
- `t3 <overlay> agent [TASK]` — launch Claude Code with overlay context
- `t3 <overlay> resetdb` — drop and recreate SQLite database
- `t3 <overlay> worker` — start background task workers (singleton — refuses a second instance while one is alive; uses `teatree.utils.singleton`, a non-blocking `flock` over `$XDG_DATA_HOME/teatree/teatree-worker.pid`)

**Management command groups** (each exposed as a sub-typer):

`lifecycle`, `workspace`, `run`, `db`, `pr`, `tasks`, `followup` — see §8.1 for details.

### 8.4 Overlay Contract Check (`t3 overlay contract-check`)

`contract-check --compose <paths>` reads every `${VAR}` reference in the listed docker-compose files and fails if any reference is neither defaulted (`${VAR:-x}`, `${VAR:?x}`) nor declared by core (`_declared_core_keys()`) or the active overlay (`OverlayBase.declared_env_keys()`). Stops the "compose references a missing key, substitutes empty string, something misbehaves quietly" class of bug at CI time. Overlay repos wire this into their own prek hook. The underlying utility is `teatree.utils.compose_contract` — same logic lives in `tests/test_env_contract.py` for the core repo's own compose files.

### 8.5 Overlay Dev Loop (`t3 overlay install|uninstall|status`)

Ships alongside the three-tier split above. Purpose: in a teatree feature worktree (never the main clone), editable-install a sibling overlay checkout so the `t3` CLI and agents immediately see unreleased teatree code plus the overlay that exercises it.

- `install <name>` walks up from `cwd` to find the teatree worktree, resolves the overlay main clone via `[overlays.<name>].path` in `~/.teatree.toml`, adds a sibling `git worktree` matching the teatree branch (falls back to the overlay's default branch), then runs `uv pip install --editable --no-deps <sibling>` against the teatree worktree venv. State is persisted in `.t3.local.json` (gitignored).
- `uninstall <name>` removes the overlay from the venv and state file.
- `status` lists overlays tracked in `.t3.local.json`.

Refuses to run in the main clone (detected via a real `.git` directory). Tests in the teatree worktree stay deterministic because `tests/conftest.py` pins `T3_OVERLAY_NAME=t3-teatree`.

`TeatreeOverlay.get_provision_steps()` automates the same install for discovered overlays: after `uv sync`, an `install-overlays-editable` step iterates `discover_overlays()` and runs `uv pip install -e <overlay_worktree>` for each entry whose main `project_path` resolves inside the user's `workspace_dir`. Overlays outside `workspace_dir`, overlays without a sibling worktree under the ticket dir, and the teatree overlay itself (already handled by `uv sync`) are silently skipped — the installed package is the fallback.

### 8.6 Teatree Source Resolution in Overlay Projects

Overlay projects depend on `teatree` as a Python package. The `[tool.uv.sources]` entry in `pyproject.toml` always points to a **local relative path**:

```toml
[tool.uv.sources]
teatree = { path = "../../souliane/teatree", editable = true }
```

This is the committed default — no SHA pinning, no mode switching.

**Local dev:** teatree is already checked out at the expected relative path (`../../souliane/teatree` from the overlay project root). `uv sync` resolves it as an editable install. Changes to teatree code are immediately visible without re-installing.

**CI:** the overlay's CI workflow clones teatree at the same relative path before `uv sync`:

```yaml
# .github/workflows/ci.yml — add to every job, before setup-uv
- uses: actions/checkout@v6
  with:
    repository: souliane/teatree
    path: teatree-upstream
- run: mkdir -p ../../souliane && ln -s "$GITHUB_WORKSPACE/teatree-upstream" ../../souliane/teatree
```

CI always tests against teatree's latest `main`. There is no pinned SHA to bump — overlay CI tracks teatree head, and local dev uses whatever is checked out locally.

**Picking up new teatree changes (local):**

```bash
cd ../../souliane/teatree && git pull
cd - && uv lock && uv sync
```

If `uv.lock` changes because teatree's dependencies shifted, commit the lock file.

**Why not a git rev pin?**

The previous approach (`teatree = { git = "...", rev = "<SHA>" }` committed, `skip-worktree` locally) caused persistent friction:

- The SHA went stale within days, requiring manual bumps.
- `skip-worktree` / `assume-unchanged` flags were invisible and easy to forget, leading to accidental commits of local paths or accidental reversions to stale SHAs.
- `t3 doctor make_editable()` existed to auto-fix but wasn't reliably triggered.
- Debugging "which teatree version is this?" required checking both the committed SHA and the local override state.

The local-path-first approach eliminates all of these. CI clones fresh on every run, so determinism comes from the CI workflow (always `main` HEAD), not from a pinned SHA that drifts.

**Pitfalls:**

- `path = "."` is wrong — it points at the overlay itself, causing a name mismatch error.
- `pyproject-fmt` may reformat the source entry — verify after running pre-commit.
- The relative path `../../souliane/teatree` assumes the standard workspace layout where both repos live under the same parent. Adjust if the layout differs.
- For private overlays on GitHub Actions, the teatree checkout step needs no extra auth (teatree is public). For private teatree forks, add a PAT to the checkout step.
