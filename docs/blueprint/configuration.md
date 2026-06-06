# BLUEPRINT Appendix — Configuration

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §10. Consumer cross-references such as `BLUEPRINT §10.1` (~/.teatree.toml, slack-bot setup) resolve here.

## 10. Configuration

### 10.1 ~/.teatree.toml

```toml
[teatree]
workspace_dir = "~/workspace"
branch_prefix = ""
privacy = "strict"
mode = "interactive"                       # global default — confirm before publishing actions. Per-overlay override to "auto" enables loop-driven autonomy.
loop_cadence_seconds = 720                 # /loop tick interval (default 12 min)
require_human_approval_to_merge = true     # training-wheel for `auto` overlays: push + PR create autonomous, merge stays gated
require_human_approval_to_answer = true    # training-wheel: t3:answerer drafts + DMs for approval, posts only on confirm
on_behalf_post_mode = "draft_or_ask"       # tri-state pre-gate (#960): draft_or_ask (default; draft notes publish autonomously, every other action BLOCKs identical to ask) | ask (every action BLOCKs) | immediate (gate off)
notify_on_post_on_behalf = true            # DM the user after every on-behalf post (#949)
user_identity_aliases = []                 # cross-platform handles for the same human (#975/#976); consumed by TicketDispositionScanner + multi-identity scanning
statusline_chain = []                      # extra statusline scripts (glob patterns) chained after the loop's zones
repo_mode = ""                             # solo/collaborative working mode (#550 item 4); "" = auto-detect from git shortlog history
claude_chrome = true                       # spawn `claude` with --chrome so sessions can drive the browser
agent_signature = false                    # never append agent identity (Co-Authored-By, "Sent using …") to user-on-behalf posts
max_concurrent_local_stacks = 0            # #1397: cap on concurrent locally-running stacks per overlay (0 = unbounded, default)

[overlays.myproject]
path = "~/workspace/myproject"
code_host = "github"                       # "github" | "gitlab"
messaging_backend = "slack"                # "slack" | "noop" (default)
slack_token_ref = "teatree/slack/myproject"   # `pass` entry prefix; -bot and -app suffixes resolve the two tokens
user_token_ref = "slack/user-oauth-token"  # optional; `pass` entry holding the human's xoxp token (routes posts AND reactions on Slack-Connect channels where the bot token is rejected; internal channels/DMs stay on the bot token)
slack_user_id = "U01ABCD1234"              # my Slack user ID (used to filter mentions/DMs)

[overlays.another-project]
path = "~/workspace/another-project"
code_host = "gitlab"
messaging_backend = "slack"
slack_token_ref = "teatree/slack/another-project"
slack_user_id = "U01ABCD1234"

# External Playwright E2E repos — used by `t3 e2e external --repo <name>`
# Teatree clones/updates the repo to ~/.local/share/teatree/e2e-repos/<name>/
# and runs Playwright from <clone>/<e2e_dir>.
[e2e_repos.my-service]
url = "git@gitlab.com:org/my-service.git"
branch = "feature/e2e-tests"
e2e_dir = "e2e"  # subdirectory containing playwright.config.ts (default: "e2e")

```

**Slack bot setup** (`t3 setup slack-bot --overlay <name>`): an interactive walkthrough scaffolds the per-overlay Slack app and stores its tokens. Steps:

1. Print the manifest JSON (with `messages_tab_enabled`, `app_mentions:read` scope, Socket Mode, bot events `app_mention` + `message.im`) and open the Slack app creation page. The user pastes the manifest, creates the app, installs it to the workspace, and generates an app-level token with `connections:write` scope.
2. Capture the bot token (`xoxb-…`) and app-level token (`xapp-…`) into `pass` entries `<slack_token_ref>-bot` and `<slack_token_ref>-app`.
3. Auto-detect the user's Slack ID from `git config user.email` via the Slack API. Falls back to a manual prompt when detection fails.
4. Write `messaging_backend`, `slack_user_id`, and `slack_token_ref` to `[overlays.<name>]` in `~/.teatree.toml`.
5. Smoke-test by sending a DM via the bot and waiting for the user to react with ✅.

The walkthrough never writes a bot token to disk in plaintext; tokens always go via `pass`. Re-running `t3 setup slack-bot --overlay <name> --reset` rotates both tokens but **skips the manifest** — it does **not** apply a scope change.

For an **existing** app, the command updates its manifest in place. When `[overlays.<name>].slack_app_id` is recorded (the create flow prompts and persists it) — or `--update` is passed (prompting for the app id when none is recorded) — it calls Slack's `apps.manifest.export` / `apps.manifest.update` using org-wide config tokens in `pass` (`teatree/slack-app-config-token`, `teatree/slack-app-config-refresh`; auto-rotated via `tooling.tokens.rotate` on `invalid_auth`/`token_expired`). A matching manifest is an idempotent no-op; otherwise it is applied and the **single** remaining manual step is the browser OAuth-consent reinstall click at the deep link (`https://api.slack.com/apps/<app_id>/install-on-team`). With no config token stored it degrades: prints the manifest plus the manifest-editor deep link for a manual paste, then smoke-tests with the stored bot token. Adding/changing a manifest scope (e.g. granting the xoxp user token `reactions:write`) requires a full reinstall via this path so Slack re-prompts OAuth consent for the new scope set.

**One-command full setup** (`t3 setup slack-provision [--overlay <name>]`): runs the entire Slack lifecycle for one overlay — or every `messaging_backend = "slack"` overlay when `--overlay` is omitted — in one idempotent pass, replacing the `slack-bot` + `slack-user-token` + manual-channel-invite sequence ([#1686](https://github.com/souliane/teatree/issues/1686)). Per overlay it: resolves the app id (config → derive from bot token → prompt, persisted via the shared `slack_app_resolve` helper that also stops `slack-bot --update` prompting); pushes the manifest (all bot + user scopes incl. `reactions:write`) via `apps.manifest.update`; prints + opens the OAuth (re)install URL (the one manual step — `--no-open-browser` suppresses); joins the bot to its review-broadcast channels via `conversations.join` so its first post/reaction does not fail `not_in_channel` (private/Connect channels print a manual `/invite` line); provisions the bot IM channel; and verifies the shared xoxp token carries every required scope. Never deletes credentials; safe to re-run.

**Socket Mode listener** (`t3 slack listen`): a global singleton process that opens one WebSocket per slack-enabled overlay. Events are written to `$XDG_DATA_HOME/teatree/slack-events.jsonl` in real time. `t3 slack status` checks if the listener is running. `t3 slack check` drains the queue and prints user messages as JSON (exit 0 = messages found, 1 = empty) — designed for a fast cron (30s–1min). The listener uses the shared `teatree.utils.singleton` flock primitive (kernel-enforced, crash-safe) — only one instance runs at a time. Start it as a background process or let the SessionStart hook manage its lifecycle.

**Operating mode (`teatree.mode`, env: `T3_MODE`)** — controls whether the agent
pauses for confirmation on publishing actions (push, PR create, PR merge, messaging-backend
posts, remote branch deletion):

| Mode | Default | Meaning |
|------|---------|---------|
| `interactive` | ✅ | Canonical default. Confirm before push, PR create, messaging-backend posts, any remote write. Always-gated destructive ops (force-push to default branches, history rewrites on shared defaults, destructive DB ops on non-ticket schemas, unauthorized external writes) stay gated regardless of mode. |
| `auto` |  | Opt-in per overlay. End-to-end autonomy: push, PR create, clean-all's branch pruning, retro writes, overlay-approved messaging-backend posts run without prompts. Merge is gated by `require_human_approval_to_merge` (default `true`). Always-gated destructive ops still apply. Recommended for personal dogfooding overlays where the user accepts the trust boundary; use `interactive` for client / shared-team overlays. |

The env var `T3_MODE` overrides the toml setting. Unknown values raise
`ValueError` — typos never silently downgrade to a less-safe mode.

### 10.1.1 Per-Overlay Setting Overrides

A subset of `[teatree]` keys can be overridden per-overlay in
`[overlays.<name>]`. The resolution chain (first match wins):

1. `T3_*` env var (wired one-offs in `ENV_SETTING_OVERRIDES`: `T3_MODE`, `T3_SPEED`, `T3_ON_BEHALF_POST_MODE`, `T3_REVIEW_SKILL`).
2. Active overlay's override from `[overlays.<name>]`.
3. Global `[teatree]` value.
4. `UserSettings` dataclass default.

The active overlay is resolved via (in order): `T3_OVERLAY_NAME` env var
(runtime truth; matches `get_overlay()`), cwd-based discovery, then the
single installed overlay.

Overridable keys live in `OVERLAY_OVERRIDABLE_SETTINGS` in
`src/teatree/config.py`. The registry is the single source of truth — the table
below mirrors it; consult the dataclass for type signatures and defaults.

| Key | Why overridable |
|-----|------------------|
| `mode` | `auto` for a personal dogfooding overlay, `interactive` for a client overlay |
| `autonomy` | Single trust switch, tiers `full > notify > babysit` (default `babysit`). Both autonomous tiers collapse the three approval gates (colleague auto-approve via `on_behalf_post_mode`, auto-merge, auto-answer) and pin `mode = auto`; `full` enables the single-author `solo_overlay` merge bypass, `notify` derives `notify_on_behalf = true` and keeps the colleague-approval CLEAR merge path. An explicit per-gate value wins, and a global `mode` does not defeat the `mode = auto` pin (a per-overlay one does). Set without hand-editing TOML via `t3 <overlay> autonomy set <tier>` (`--overlay <name>` / `--global`); `t3 <overlay> autonomy show` reports the effective tier. Safety floor untouched |
| `speed` | Throughput dial `slow < medium < full < boost` (default `medium`): how many threads run at once, orthogonal to `mode`/`autonomy`. `t3 <overlay> speed set`; `T3_SPEED` env. |
| `branch_prefix` | Different prefix conventions per project |
| `privacy` | Stricter for client code, looser for personal |
| `contribute` | Contribute to one overlay's skills but not another |
| `excluded_skills` | Project-specific skill exclusions |
| `loop_cadence_seconds` | Per-overlay tick cadence (e.g. tighter on a hot overlay, looser on a maintenance one) |
| `require_human_approval_to_merge` | Training-wheel: auto-mode overlay can publish autonomously, merge stays gated |
| `require_human_approval_to_answer` | Training-wheel for `t3:answerer`: drafts + DMs, posts only on confirm |
| `ask_before_post_on_behalf` | Legacy boolean pre-gate over on-behalf posts (kept for back-compat — prefer `on_behalf_post_mode`) |
| `on_behalf_post_mode` | Tri-state pre-gate (#960): `draft_or_ask` / `ask` / `immediate`, scoped per overlay so a client overlay can stay `ask` while a personal one runs `immediate` |
| `notify_user_via_bot` | Whether the bot→operator `notify_user(...)` channel (#963) DMs the user via the overlay's Slack bot (out of scope for the on-behalf gates — see config.py for the boundary) |
| `notify_on_post_on_behalf` | DM the user after every on-behalf post (#949) — per-overlay because noise tolerance differs |
| `user_identity_aliases` | Per-overlay handles (e.g. different GitHub login on a client overlay), consumed by §5.6 scanners (#975/#976) |
| `architectural_review_disabled` | Escape hatch for the periodic architectural-review scanner on a given overlay |
| `architectural_review_skill` | Override which skill the scanner dispatches (default `/ac-reviewing-codebase`) |
| `architectural_review_cadence_hours` | Per-overlay cadence floor for the architectural-review scanner |
| `architectural_review_after_merge_count` | Per-overlay merge-count trigger for the architectural-review scanner |
| `review_skill` | #1539: per-ticket deep-review skill (env `T3_REVIEW_SKILL`). Empty (default) ⇒ reviewing-phase gate is a NO-OP; when set, `visit-phase … reviewing` needs a `review_skill_run` artifact. |
| `scanning_news_disabled` | Escape hatch for the daily `t3:scanning-news` scanner (#1191) — registered as overridable, but the live scanner reads the global `[teatree]` value (the news-scan is anchored on the `teatree` overlay placeholder ticket; per-overlay overrides are accepted in the registry but not yet consumed by `_scanning_news_scanner` in `loop/global_scanner_factories.py`) |
| `scanning_news_skill` | Override which skill the scanner dispatches (default `/t3:scanning-news`) — same registry/consumer gap as above |
| `scanning_news_cadence_hours` | Cadence floor for the news-scanning scanner — same registry/consumer gap as above |
| `eval_local_disabled` | Escape hatch for the periodic local-eval scanner (`eval_local`). The loop fires a weekly `eval_local` task so the SCOPED eval suite runs locally via the no-API-key subscription runner (the local half of "evals run locally + in CI weekly"; CI half is `eval-weekly`). |
| `eval_local_skill` | Override which skill the eval-local scanner dispatches (default `eval`) |
| `eval_local_cadence_hours` | Cadence floor for the local-eval scanner (default 168 = weekly) |
| `max_concurrent_local_stacks` | #1397: cap on concurrent locally-running stacks per overlay (0 = unbounded). A heavy overlay caps to `1` while a cheap dogfood overlay stays unbounded; enforced by `t3 <overlay> worktree start` / `workspace start` |
| `orchestrator_bash_gate_enabled` | #115: kill-switch (default `true`) for the §17.6.4 gate 2 (`handle_enforce_orchestrator_boundary`). When on, the MAIN agent is blocked from running a LONG / HEAVY foreground `Bash` command (test suite, build, dev server, long sleep, full-tree sweep); `run_in_background: true` is the escape hatch, sub-agents unrestricted. Set `false` under `[teatree]` (read directly by the hook layer, mirroring `_plan_gate_enabled`) or per-overlay to disable it — e.g. as the failsafe after `t3 update` reinstalls the gate. |
| `orchestrator_turn_budget` | Soft per-turn tool-call budget (default `25`; `0` disables) for the §17.6.4 gate 2 responsiveness nudge (`handle_orchestrator_turn_budget_nudge`). Governs long TURNS (vs the heavy-`Bash` arm's long OPERATIONS) — once a MAIN-agent turn makes this many NON-orchestration tool calls, a one-time `additionalContext` line steers it to yield. Advisory only (never a deny); orchestration calls and sub-agents exempt. |
| `skill_loading_gate_enabled` | #1488: kill-switch (default `true`) for the §17.6.4 skill-loading gate that blocks `Bash`/`Edit`/`Write` and the fanned-out `TaskCreated` counterpart until the resolvable pending teatree skills load. Read directly by the hook layer (mirroring `_plan_gate_enabled`); set `false` under `[teatree]` or per-overlay, or disable via `t3 <overlay> gate skill-loading disable`. |
| `mcp_privacy_gate_enabled` | #171: canary off-switch (default `true`) for the Slack-MCP arm of the #1213 quote-scanner and #1218 bare-reference publish-privacy gates (reachable via the `mcp__.*[Ss]lack.*` matcher). Fails OPEN; set `false` to disable the Slack-MCP arm alone if it misfires. The Bash arm of both gates is unaffected. |
| `dispatch_quote_gate_on_task_create_enabled` | #171: opt-in switch (default `false`) for the `TaskCreated` dispatch-quote gate (`handle_dispatch_prompt_quote_scanner_on_task_create`) — the fan-out counterpart of the `PreToolUse` dispatch-quote gate (the `Task`/`Workflow` fan-out bypasses `PreToolUse`, so only `TaskCreated` reaches a fanned-out dispatch). Fails CLOSED (unvalidated fan-out gate stays inert by default); set `true` to scan fanned-out task subjects/descriptions for HIGH verbatim user quotes. Clears on a `[quote-ok: <reason>]` token. |
| `orchestrator_boundary_agent_gate_enabled` | #171: opt-in switch (default `false`) for the `Agent` arm of the §17.6.4 gate 2 (`handle_enforce_orchestrator_boundary` → `_deny_foreground_agent_dispatch`, #1442), denying a main-agent FOREGROUND `Agent` dispatch. Currently dead (no `Agent` matcher wired in `hooks.json`); ships default-OFF because enabling it would block the loop's own foreground dispatches — a lockout risk to validate attended ([#1646](https://github.com/souliane/teatree/issues/1646)). Fails CLOSED; off-ramps when enabled: sub-agent context, `run_in_background: true`, per-call `[fg-ok: <reason>]`. See the hooks CLAUDE.md for the matcher/fan-out rationale. The `Bash` arm (`orchestrator_bash_gate_enabled`) is unaffected. |
| `danger_gate_fail_open` | NEVER-LOCKOUT switch (default `false`): `true` flips every over-deny gate to fail-open. PUBLIC-egress gate excluded. The `danger_` prefix flags that a forgotten `true` override silently disables protective gates. See BLUEPRINT §17 invariant 10. |
| `mr_title_regex` | #1540: MR title pattern the `pr create` gate enforces (default Conventional Commits); an overlay declares its own grammar. The gate also requires a What/Why description, no bypass. |
| `private_repos` | Offline slug-SUBSTRING allowlist of known-private repos. Drives the #126/#1657 carve-out and (unioned with `internal_publish_namespaces`, #1672) the destination skip, so a user with only this set needs no second list. `teatree.hooks._repo_visibility`. |
| `internal_publish_namespaces` | Destination allowlist (default `[]`) making the #1415/#1530 publish gates destination-aware: a target that prefix-matches is internal and skipped. #1672 unions it with `private_repos`, deciding the skip PER top-level segment — a chained/substituted public post or a raw-REST `api` segment forces the whole command SCANNED. FAIL-CLOSED (empty/unresolvable stay PUBLIC). `teatree.hooks.publish_destination`; env `T3_INTERNAL_PUBLISH_NAMESPACES` supplements. |
| `speak` | #2060: text-to-speech `[teatree.speak]` sub-table — `local` enum (`off`/`dm`/`all`) + `slack` bool. See §10.1.1. |

`notify_on_behalf` is NOT in this registry — it is derived (read-only),
set by `_apply_autonomy` under `autonomy = "notify"`, never a user toml key.

### 10.1.1 Local text-to-speech (#2060)

The `[teatree.speak]` sub-table reads agent output aloud, gated on the macOS
`say` binary (the whole feature is inert when it is absent). Per-overlay
overridable via `[overlays.<name>.speak]`; ad-hoc local read via `t3 speak "…"`.

```toml
[teatree.speak]
local = "all"           # "off" (default) | "dm" | "all" — what plays through this machine's speakers
slack = true            # false (default) | attach a spoken audio file to each Slack DM you receive
```

`local` is what plays through the speakers (macOS `say`): `off` nothing, `dm`
the bot→user DM texts, `all` those DM texts **and** the Stop-hook reading of
in-client turn ends. `slack` attaches the spoken audio to each bot→user DM in
the **same message** (text + inline player, one DM) via
`SlackBotBackend.post_audio_dm` — no separate audio-only post; it applies to
DMs only by nature. This needs the token's **`files:write`** scope (else
`ok:false` / `missing_scope` — the DM degrades to text-only and the failure
surfaces once per error class to the user's DM with the scope-fix hint; re-run
`t3 setup slack-bot` to grant it). Both the `notify_user` DM and the on-behalf
self-DM run through one shared `teatree.core.speak.deliver_user_dm` chokepoint.

The two axes are fully independent. Slack never auto-plays, so local playback
is never suppressed by `slack`. The Stop-hook in-client read fires whenever
`local = all` — in-client turns are never Slack messages, so there is no
double-play to suppress. No DB, no state.

Callers read `get_effective_settings().speak`. Adding a new overridable key
is a one-line registry change picked up via `dataclasses.replace`; `speak` is
the one non-generic override (its overlay sub-table merges onto the base
rather than flat-replacing).

```toml
[teatree]
mode = "interactive"         # global default
branch_prefix = "ac"

[overlays.t3-teatree]
autonomy = "full"            # single-author dogfooding: one switch collapses the gates + pins mode = auto

[overlays.t3-client]
autonomy = "notify"          # collaborative: autonomous + DM per on-behalf action, keeps CLEAR merge gate

[overlays.client-project]
mode = "interactive"         # stay gated on client code (autonomy defaults to babysit)
privacy = "strict"
```

### 10.2 Django Settings (framework-level, in teatree's settings.py)

| Setting | Type | Purpose |
|---------|------|---------|
| `TEATREE_HEADLESS_RUNTIME` | str | Runtime for headless tasks (default: "claude-code") |
| `TEATREE_CLAUDE_STATUSLINE_STATE_DIR` | str | Directory for Claude Code's per-session statusline state files used by `agents/handover.py` (default: `/tmp/claude-statusline`). Distinct from the loop's rendered statusline file — see env var `TEATREE_STATUSLINE_FILE` below. |
| `TEATREE_EDITABLE` | bool | Declare teatree is editable (verified by `t3 doctor check`) |
| `OVERLAY_EDITABLE` | bool | Declare overlay is editable (verified by `t3 doctor check`) |

### 10.2.1 OverlayBase Config Methods (`OverlayConfig`)

Overlay-specific configuration lives on `overlay.config` (an `OverlayConfig` dataclass attribute on `OverlayBase`) and on a few overlay-class properties. Backends auto-configure from these (see § 7).

**Code host** — exactly one of `github` / `gitlab` is configured per overlay:

| Method / property | Return type | Default | Purpose |
|---|---|---|---|
| `code_host` | `Literal["github", "gitlab"]` | (required) | Selects which `CodeHostBackend` implementation the loader returns |
| `get_github_token()` | `str` | `""` | GitHub PAT (used when `code_host == "github"`) |
| `get_gitlab_token()` | `str` | `""` | GitLab PAT (used when `code_host == "gitlab"`) |
| `gitlab_url` | `str` | `"https://gitlab.com/api/v4"` | GitLab API base URL (only set for self-hosted) |
| `get_username()` | `str` | `""` | The user's handle on the active code host (used to filter "my PRs") |
| `pr_auto_labels` | `list[str]` | `[]` | Labels to apply when creating PRs |

**Messaging:**

| Method / property | Return type | Default | Purpose |
|---|---|---|---|
| `messaging_backend` | `Literal["slack", "noop"]` | `"noop"` | Selects which `MessagingBackend` the loader returns |
| `slack_token_ref` | `str` | `""` | `pass` entry prefix; `<ref>-bot` and `<ref>-app` resolve the two tokens |
| `slack_user_id` | `str` | `""` | The user's Slack ID (used to filter mentions/DMs) |
| `get_review_channel()` | `tuple[str, str]` | `("", "")` | (channel name, channel ID) for review-request messages |
| `get_transition_emojis()` | `dict[str, str]` | `DEFAULT_TRANSITION_EMOJIS` | Emoji reactions per ticket-state transition |

**Other:**

| Method / property | Return type | Default | Purpose |
|---|---|---|---|
| `known_variants` | `list[str]` | `[]` | Known tenant identifiers for `detect_variant()` |
| `frontend_repos` | `list[str]` | `[]` | Repos whose changes trigger frontend-flavored CI gates |
| `dev_env_url` | `str` | `""` | Dev/staging environment URL (used in PR descriptions) |
| `plan_gate` | `bool` | `False` | Retired — the wall-clock PreToolUse plan-gate was replaced by the `PLANNED` FSM state. This field is kept for migration compatibility only; no handler reads it. Plan enforcement now lives in the Ticket state graph (STARTED → PLANNED → CODED via `PlanArtifact`). |

### 10.3 Logging

`default_logging(namespace)` in `config.py` returns a Django `LOGGING` dict writing to `~/.local/share/teatree/<namespace>/logs/teatree.log` with rotation (5MB, 3 backups).

### 10.4 Data Storage

`~/.local/share/teatree/<namespace>/` — namespaced data directories created by `get_data_dir()`.

### 10.5 State Placement Rule — Cache vs Intent (#628)

**The text files are the source of truth for user *intent*; the DB caches *derived* state.** A datum may live DB-only **iff it can be deleted and deterministically rebuilt** from the text files (`~/.teatree.toml`, overlay config) plus repo state — deleting the DB must lose no user intent. If losing a datum would lose user intent, it stays text-file source-of-truth (the DB may cache a read view, never own it). The DB stays rebuildable from the text files indefinitely — no one-way migration.

Consequences: bootstrap config (DB path, log level, the `mode` resolution chain) and user-authored intent (push mode, contribute, banned terms) stay in text files — they must resolve with the DB absent. Derived/observational state (cached env values, last-seen branch, lifecycle phase history) is DB-as-cache and carries a regeneration path. A DB-only user-*intent* field (e.g. #627 `Ticket.context`) is permitted **only** with a round-trip affordance so the `cat ~/.teatree.toml` affordance is not lost — `t3 config show` is that affordance: a read-only view partitioning text-file intent from DB regenerable cache, working with the DB absent.
