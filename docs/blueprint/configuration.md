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

For an **existing** app, the command updates its manifest in place rather than re-running the create flow. When `[overlays.<name>].slack_app_id` is recorded (the create flow now prompts for it and persists it) — or `--update` is passed (prompting for the app id when none is recorded) — the command calls Slack's `apps.manifest.export` / `apps.manifest.update` using org-wide config tokens stored in `pass` (`teatree/slack-app-config-token` and `teatree/slack-app-config-refresh`; rotated automatically via `tooling.tokens.rotate` on `invalid_auth`/`token_expired`). If the desired manifest matches the live one it is an idempotent no-op; otherwise the manifest is applied and the **single** remaining manual step is the browser OAuth-consent reinstall click at the app-specific deep link (`https://api.slack.com/apps/<app_id>/install-on-team`). When no config token is stored the command degrades: it prints the manifest plus the app's manifest-editor deep link for a manual paste, then still smoke-tests with the stored bot token. Adding or changing a manifest scope (e.g. granting the xoxp user token `reactions:write`) requires a full reinstall via this manifest-update path so Slack re-prompts OAuth consent for the new scope set.

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

1. `T3_*` env var (wired one-offs in `ENV_SETTING_OVERRIDES`: `T3_MODE`, `T3_ON_BEHALF_POST_MODE`, `T3_REVIEW_SKILL`).
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
| `scanning_news_disabled` | Escape hatch for the daily `t3:scanning-news` scanner (#1191) — registered as overridable, but the live scanner reads the global `[teatree]` value (the news-scan is anchored on the `teatree` overlay placeholder ticket; per-overlay overrides are accepted in the registry but not yet consumed by `_scanning_news_scanner` in `loop/tick_jobs.py`) |
| `scanning_news_skill` | Override which skill the scanner dispatches (default `/t3:scanning-news`) — same registry/consumer gap as above |
| `scanning_news_cadence_hours` | Cadence floor for the news-scanning scanner — same registry/consumer gap as above |
| `max_concurrent_local_stacks` | #1397: cap on concurrent locally-running stacks per overlay (0 = unbounded). A heavy overlay caps to `1` while a cheap dogfood overlay stays unbounded; enforced by `t3 <overlay> worktree start` / `workspace start` |
| `orchestrator_bash_gate_enabled` | #115: kill-switch (default `true`) for the §17.6.4 gate 2 (`handle_enforce_orchestrator_boundary`). When on, the MAIN agent is blocked from running a LONG / HEAVY foreground `Bash` command (test suite, build, dev server, long sleep, full-tree sweep); `run_in_background: true` is the escape hatch, sub-agents unrestricted. Set `false` under `[teatree]` (read directly by the hook layer, mirroring `_plan_gate_enabled`) or per-overlay to disable it — e.g. as the failsafe after `t3 update` reinstalls the gate. |

Callers use `get_effective_settings()` (returns a `UserSettings` with the
active overlay's overrides applied) instead of reaching into
`load_config().user` directly. Adding a new overridable key is a
one-line change to the registry — the resolver picks it up via
`dataclasses.replace`, no per-setting getter needed.

```toml
[teatree]
mode = "interactive"         # global default
branch_prefix = "ac"

[overlays.t3-teatree]
mode = "auto"                # auto-mode for the t3-teatree dogfooding overlay

[overlays.client-project]
mode = "interactive"         # stay gated on client code
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
| `plan_gate` | `bool` | `False` | Opt this overlay into the PreToolUse plan-gate ([#1133](https://github.com/souliane/teatree/issues/1133)): when ANY overlay has `plan_gate = true` in `~/.teatree.toml`, `hook_router.handle_enforce_plan_gate` denies `Edit`/`Write` on files under `$T3_WORKSPACE_DIR` unless the session has invoked `/plan` (recorded in `<session>.plan-invocations`) or already `Read` the touched file (recorded in `<session>.workspace-reads`). Outside the workspace (e.g. `~/.zshrc`, `~/.claude/`, agent memory) the gate passes through silently. Default OFF — the gate is silent until at least one overlay opts in. |

### 10.3 Logging

`default_logging(namespace)` in `config.py` returns a Django `LOGGING` dict writing to `~/.local/share/teatree/<namespace>/logs/teatree.log` with rotation (5MB, 3 backups).

### 10.4 Data Storage

`~/.local/share/teatree/<namespace>/` — namespaced data directories created by `get_data_dir()`.

### 10.5 State Placement Rule — Cache vs Intent (#628)

**The text files are the source of truth for user *intent*; the DB caches *derived* state.** A datum may live DB-only **iff it can be deleted and deterministically rebuilt** from the text files (`~/.teatree.toml`, overlay config) plus repo state — deleting the DB must lose no user intent. If losing a datum would lose user intent, it stays text-file source-of-truth (the DB may cache a read view, never own it). The DB stays rebuildable from the text files indefinitely — no one-way migration.

Consequences: bootstrap config (DB path, log level, the `mode` resolution chain) and user-authored intent (push mode, contribute, banned terms) stay in text files — they must resolve with the DB absent. Derived/observational state (cached env values, last-seen branch, lifecycle phase history) is DB-as-cache and carries a regeneration path. A DB-only user-*intent* field (e.g. #627 `Ticket.context`) is permitted **only** with a round-trip affordance so the `cat ~/.teatree.toml` affordance is not lost — `t3 config show` is that affordance: a read-only view partitioning text-file intent from DB regenerable cache, working with the DB absent.
