# BLUEPRINT Appendix — Configuration

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §10. Consumer cross-references such as `BLUEPRINT §10.1` (~/.teatree.toml, slack-bot setup) resolve here.

## 10. Configuration

### 10.1 ~/.teatree.toml

```toml
# ~/.teatree.toml holds ONLY the TOML-home carve-out + overlay discovery /
# messaging / raw-table keys (#1775). The operational knobs — mode, the
# approval gates, on_behalf_post_mode, repo_mode, the cadence/threshold dials,
# … — are DB-home and live in the ConfigSetting store; a value for one of them
# left in [teatree] / [overlays.<name>] is IGNORED on read. Set them with
# `t3 <overlay> config_setting set` (see below). `t3 setup` auto-migrates an
# existing config into the store on every run (non-clobbering: it seeds only keys
# absent from the store, so a value you later change via `config_setting set`
# survives); `t3 <overlay> config_setting import` is the manual equivalent (it
# refreshes every operational key from the file).
[teatree]
workspace_dir = "~/workspace"
privacy = "strict"
statusline_chain = []                      # extra statusline scripts (glob patterns) chained after the loop's zones (read by the bash statusline hook)
orchestrator_bash_gate_enabled = true      # #115 kill-switch, read directly by the hook layer (pre-Django)

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
# and runs Playwright from <clone>/<e2e_dir>. `branch` is the default ref;
# `--branch <name>` (alias `--ref`) overrides it to run the suite from an open
# MR's source branch instead.
[e2e_repos.my-service]
url = "git@gitlab.com:org/my-service.git"
branch = "feature/e2e-tests"
e2e_dir = "e2e"  # subdirectory containing playwright.config.ts (default: "e2e")

# Cross-repo "my open MRs" Slack reminder — used by `t3 <overlay> mr_reminder`.
# Routes each open MR/PR to a channel by its repo slug (most-specific match
# wins; an org-namespace prefix like "acme-engineering" routes acme-engineering/*).
[mr_reminder]
default_channel = "C_FALLBACK"        # optional: channel for an MR matching no pattern (omit to drop unrouted)
[mr_reminder.channels]
"souliane/teatree" = "C_TEATREE"      # exact owner/repo → channel id (or #channel-name)
"acme-engineering" = "C_ACME"         # org-namespace prefix → channel for every repo under it

```

The operational (DB-home) settings are set in the store, not the file above —
globally, or scoped to one overlay with `--overlay <name>`:

```bash
t3 <overlay> config_setting set mode auto                                  # global default
t3 <overlay> config_setting set require_human_approval_to_merge false --overlay myproject
t3 <overlay> config_setting set on_behalf_post_mode immediate
t3 <overlay> config_setting set user_identity_aliases '["handle-a", "handle-b"]'
t3 <overlay> config_setting import                                         # manual one-time migrate (refreshes from file); `t3 setup` runs the non-clobbering auto-migration
```

**Cross-repo "my open MRs" reminder** (`t3 <overlay> mr_reminder`): generalises a
personal one-off reminder into a reusable command. It lists every open MR/PR the
user authors across all repos one code-host token can see (union-queried across
`user_identity_aliases`, deduped by URL), routes each to a Slack channel via the
`[mr_reminder]` repo→channel map, and assembles one mrkdwn message per channel.
`preview` is read-only; `send` posts one message per routed channel. Routing reuses
the same host-stripped leading-segment-prefix grammar as `private_repos`
(`teatree.hooks._repo_visibility.slug_namespace_matches`): the most-specific
configured pattern wins, and an unmatched MR falls back to `default_channel`
(empty `default_channel` keeps it out of every channel rather than guessing). The
assembly + routing are pure (`teatree.core.mr_reminder`); the per-channel post routes
through the on-behalf egress chokepoint (`OnBehalfSlackEgress.post`), so a reminder
channel (a colleague surface) is gated + audited like any on-behalf post.

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

**Setting-home partition ([#1775](https://github.com/souliane/teatree/issues/1775)).**
Every non-derived `UserSettings` field has EXACTLY ONE home, declared in the
typed registry `config/homes.py` (`SettingHome` ∈ {`DB`, `TOML`}, `SETTING_HOMES`):
a field that CAN live in the DB is **DB-home**, and only the irreducible carve-out
stays **TOML-home**. The two homes are disjoint (a fitness function asserts it) — a
setting is never read from both tiers. A DB-home field resolves from `ConfigSetting`
(global + overlay rows) + env only; a TOML-home field resolves from `[teatree]` +
`[overlays.<name>]` + env only. The TOML carve-out is the eleven fields a non-Django
or pre-Django reader needs (`orchestrator_bash_gate_enabled`, `speak`,
`handover_mirror_path`, `check_updates`, and `statusline_chain` — the latter read
straight from `~/.teatree.toml` by the **bash** statusline hook, which has no path
to the DB), path/infra bootstrap (`workspace_dir`, `worktrees_dir`,
`redis_db_count`, `timezone`, `privacy`), and the nested structured `mr_reminder`
table. The two DERIVED fields (`notify_on_behalf`, `ask_before_post_on_behalf`) are
computed by the resolver and have no home. The resolution-tier wiring below details
each home's chain.

The resolution chain is **per home** (first match wins) — each field reads from
exactly the tiers its home allows:

* **DB-home field** (every field not in the carve-out): `T3_*` env var (wired one-offs
  in `ENV_SETTING_OVERRIDES`: `T3_MODE`, `T3_SPEED`, `T3_ON_BEHALF_POST_MODE`,
  `T3_MISSING_ISSUE_POLICY`, `T3_REVIEW_SKILL`), then the **DB store** — an
  **overlay-scoped** `ConfigSetting` row, then a **global** row — then the
  `UserSettings` dataclass default. Its `[teatree]` / `[overlays.<name>]` TOML
  value is **ignored on read** (left over from a pre-partition config, it is
  migrated into the store with `t3 <overlay> config_setting import`).
* **TOML-home field** (the carve-out): `T3_*` env var, then the active overlay's
  `[overlays.<name>]` override, then the global `[teatree]` value, then the
  dataclass default. A `ConfigSetting` row for it is **ignored on read** (and
  `config_setting set` refuses to write one).

The DB store (`core.models.ConfigSetting`, `db_table=teatree_config_setting`) is
the canonical-tier-is-the-DB pattern (`MergeClear` / `DbApproval`): for a DB-home
field it is the SOLE authoritative tier below env. An **empty table resolves every
DB-home field to its dataclass default** — `resolution._db_setting_overrides()`
returns `{}` — and the read **fails safe to `{}`** for INFRASTRUCTURE failures
(Django unconfigured or the table missing pre-migration), so this hot config path
never raises on a missing table. The store is scoped to keys registered in
`OVERLAY_OVERRIDABLE_SETTINGS` (exactly the DB-home set), coercing the stored JSON
value with that registry's parser; a row for any other key is ignored.

**Per-overlay + global DB scope.** Each `ConfigSetting` row carries a `scope`,
mirroring the TOML two-tier shape in the database: the empty-string scope
(`scope = ""`) is the **global** scope — the row applies to every overlay, the
original single-tier behaviour — and a non-empty `scope` is an **overlay name**
(the same identifier as `[overlays.<name>]`) scoping the row to that overlay alone.
Uniqueness is the `(scope, key)` pair, so a global and an overlay row for the
same key coexist. `_db_setting_overrides()` layers the **global rows first, then
the active overlay's rows on top** (later wins) — so an overlay-scoped DB row
beats a global DB row, exactly as a per-overlay `[overlays.<name>]` TOML value
beats the global `[teatree]` value, and an env var still beats both. The active
overlay is resolved the same way the per-overlay TOML layer resolves it
(`overlay_name` argument on the named-overlay path the loop scanners use, else
`T3_OVERLAY_NAME` / cwd discovery), and the overlay scope is matched
**canonical-alias-tolerantly** (a row under `t3-myproject` resolves for an active
`myproject` overlay and vice versa). With no overlay-scoped rows the resolution
is byte-identical to the global-only tier, so this extension is itself a no-op
until a scoped row is written. Admin path: `config_setting set|get|clear`
take `--overlay <name>` (omitted = global); `list` names each row's scope.

The stored value is **validated at WRITE time** ([#258](https://github.com/souliane/teatree/issues/258)):
`config_setting set` runs the same registry parser before persisting, so an
out-of-enum value (a bad `mode`) or a quoted bool (`"false"` for a bool-typed
setting) is rejected loudly and never stored — a bad write can therefore never
poison reads. Bool-typed settings use a strict parser (`_parse_strict_bool`)
that accepts only real JSON/TOML booleans (`true`/`false`) and rejects a quoted
`"false"` rather than truthy-coercing it via `bool(...)` (the old `bool("false")
== True` footgun that would silently enable an opt-in safety setting). The other
typed parsers are **strict in the same spirit**: `_parse_strict_int` rejects a
JSON `true` (a `bool` is a subclass of `int`, so the old bare `int` made
`int(True) == 1` and accepted a bool for an int setting), `_parse_strict_float`
rejects a bool, `_parse_strict_str` rejects a non-string rather than stringifying
it, and `_parse_str_list` **raises** on a non-list scalar rather than silently
degrading to `[]` (the old `excluded_skills true` footgun). Write validation
**persists the canonical parsed value**, not the raw user value — a numeric
string `"5"` is stored as the int `5`, an upper-case `"AUTO"` as the normalised
`"auto"` — so the DB row and the read-time re-coercion always agree. Because
writes are validated, a per-row coercion failure at read time can only mean an
out-of-band DB corruption — so it is raised loud with the offending key named,
not silently dropped back to the file value. The spoken-DM path
(`speak._resolve_speak_safe`) honours that loudness: a `ValueError` from a corrupt
config row is logged at **error** (the text DM still degrades gracefully), never
swallowed at debug.

The model reaches the
resolver via `django.apps.apps.get_model("core", "ConfigSetting")` (a runtime
lookup, not a static import) so the `config` platform layer never takes a
backwards edge on the `core` domain layer (tach-clean). Admin path:
`t3 <overlay> config_setting set <key> <json> [--overlay <name>] | get <key> [--overlay <name>] | clear <key> [--overlay <name>] | list | import`.
`--overlay <name>` on `set`/`get`/`clear` addresses that overlay's DB scope (omit
for the global scope). `get` is the read side of the dual-read store — it prints a
setting's resolved value and names its source (`db` when a row exists, else `file/env`). `import` is
the one-time partition migration ([#938](https://github.com/souliane/teatree/issues/938)):
it seeds the store from every operational toml key that is a registered
`OVERLAY_OVERRIDABLE_SETTINGS` field (coerced through that registry's parser,
upserted so a re-run is idempotent), skipping bootstrap-file-only and unknown keys.
It walks BOTH tiers — every operational `[teatree]` key into the GLOBAL scope and
every operational `[overlays.<name>]` key into THAT overlay's scope (the DB twin of
the per-overlay TOML override), so an install with both a global and a per-overlay
value for a DB-home key migrates both in one pass. The overlay's own `path` / `url`
discovery keys are not settings and are skipped. Run it once after upgrading to the
partition so an existing install's DB-home keys keep applying (they are otherwise
ignored on read).

A DB-home key left in `[teatree]` / `[overlays.<name>]` after the migration is
ignored on read. This is loud only when it actually **conflicts** with the store:
`load_config` warns (logger `teatree.config`) for a DB-home key set to a value that
DIFFERS from its `ConfigSetting` row — the one case where the silently-ignored TOML
value disagrees with what is in effect. All such conflicts collapse into a SINGLE
`WARNING` naming every offending key, its TOML location, and the `config_setting
import` path. A DB-home key that is absent from the store (being migrated away) or
that agrees with it resolves to the same effective value and stays silent — so a
cleaned config emits zero per-key noise rather than one line per DB-homed key.

Bootstrap-readable settings (`DATABASE_URL` / data-dir / `DJANGO_SETTINGS_MODULE`
/ the offline `private_repos` allowlist) are explicitly out of scope — they must
resolve before Django starts. That boundary is a **typed allowlist**,
`BOOTSTRAP_FILE_ONLY_SETTINGS` in `config/settings.py`, not just prose: a fitness
function asserts `BOOTSTRAP_FILE_ONLY_SETTINGS ∩ OVERLAY_OVERRIDABLE_SETTINGS == ∅`,
so a bootstrap key can never be made DB-overridable (and `config_setting set` /
`import` therefore never write a row for one) without turning a test red.

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
| `on_behalf_post_mode` | Tri-state pre-gate (#960) over colleague-VISIBLE posts: `draft_or_ask` / `ask` / `immediate`, scoped per overlay so a client overlay can stay `ask` while a personal one runs `immediate`. Drafts (`post-draft-note`) are colleague-invisible and exempt under every mode — they never need approval |
| `missing_issue_ref_policy` | What to do when a commit/MR needs an issue ref and none is in hand: `find_existing_then_ask` (default) / `create` / `dummy`. Scoped per overlay so a colleague-facing client overlay stays on the never-create default while a personal one can opt into `create`. The default always recovers the original existing issue first, then ASKs on a colleague-facing repo and CREATEs on the user's own repo — never a dummy. `create` / `dummy` are opt-in and authorise auto-create / a placeholder ref on colleague repos too. Resolved by `teatree.missing_issue_policy.resolve_missing_issue_verdict`; `T3_MISSING_ISSUE_POLICY` env wins; agent prose in `skills/ship/SKILL.md` § 0a |
| `on_behalf_auto_actions` | Allowlist of on-behalf actions that PROCEED even under `ask`/`draft_or_ask` (default `["post_e2e_evidence"]`): the user's own-ticket self-documentation, not a colleague-facing voice, so they never need per-post approval. Clear to `[]` to re-gate test plan; env `T3_ON_BEHALF_AUTO_ACTIONS` (comma-separated) wins |
| `agent_review_request_disabled` | Mode-independent disable of agent-driven review-request posting (default `false`). When `true`, `resolve_on_behalf_verdict("review_request_post")` BLOCKs **regardless of `on_behalf_post_mode`** — including the `immediate` value the autonomy collapse (`notify`/`full`) forces. The customer-overlay done-definition gate: an overlay that keeps a human in the merge loop (`require_human_approval_to_merge = true`) wants the agent to stop at "MR is mergeable + review-requestable" and never auto-request review. Orthogonal to `require_human_approval_to_merge` (which gates merge, not the review-request post). Set the customer overlay `true` to disable agent review-request until it behaves correctly, `false` to re-enable. Scoped per overlay |
| `notify_user_via_bot` | Whether the bot→operator `notify_user(...)` channel (#963) DMs the user via the overlay's Slack bot (out of scope for the on-behalf gates — see config.py for the boundary) |
| `notify_on_post_on_behalf` | DM the user after every on-behalf post (#949) — per-overlay because noise tolerance differs |
| `user_identity_aliases` | Per-overlay handles (e.g. different GitHub login on a client overlay), consumed by §5.6 scanners (#975/#976) |
| `architectural_review_disabled` | Escape hatch for the periodic architectural-review scanner on a given overlay |
| `architectural_review_skill` | Override which skill the scanner dispatches (default `/ac-reviewing-codebase`) |
| `architectural_review_cadence_hours` | Per-overlay cadence floor for the architectural-review scanner |
| `architectural_review_after_merge_count` | Per-overlay merge-count trigger for the architectural-review scanner |
| `review_skill` | #1539: per-ticket deep-review skill (env `T3_REVIEW_SKILL`). Empty (default) ⇒ reviewing-phase gate is a NO-OP; when set, `visit-phase … reviewing` needs a `review_skill_run` artifact. |
| `e2e_confidence_threshold` | Rubric score (0-100, default `90`) a Playwright spec must reach to be VERIFIED by the `/t3:e2e` verify↔review loop (`/t3:e2e` § "Verify–Review Loop to Threshold"). The single knob both the `/t3:e2e-review` E2E Confidence Rubric and the loop read, so "the threshold" is one resolved value. A stricter client overlay raises it (e.g. `95`); a fast dogfood overlay lowers it. Documentation-driven today (the loop is agent prose, not a deterministic gate) — the typed field is the shared source of truth for the doc value and any future programmatic consumer. |
| `scanning_news_disabled` | Escape hatch for the daily `t3:scanning-news` scanner (#1191) — DB-home, but the live scanner reads only the GLOBAL-scope value (the news-scan is anchored on the `teatree` overlay placeholder ticket; per-overlay rows are accepted by the registry but not yet consumed by `_scanning_news_scanner` in `loop/global_scanner_factories.py`). Set it with `config_setting set scanning_news_disabled true` |
| `scanning_news_skill` | Override which skill the scanner dispatches (default `/t3:scanning-news`) — same registry/consumer gap as above |
| `scanning_news_cadence_hours` | Cadence floor for the news-scanning scanner — same registry/consumer gap as above |
| `eval_local_disabled` | Escape hatch for the periodic local-eval scanner (`eval_local`). The loop fires a weekly `eval_local` task so the SCOPED eval suite runs locally via the no-API-key subscription runner (the local half of "evals run locally + in CI weekly"; CI half is the standalone `.github/workflows/eval.yml` weekly schedule). |
| `eval_local_skill` | Override which skill the eval-local scanner dispatches (default `eval`) |
| `eval_local_cadence_hours` | Cadence floor for the local-eval scanner (default 168 = weekly) |
| `backlog_sweep_disabled` | Kill switch for the periodic backlog-sweep scanner (`backlog_sweep`, #2419) — **defaults `true` (default-OFF)** because the sweep is destructive-capable (it can propose closing issues). The loop fires a weekly `backlog_sweep` task only after the user opts in with `config_setting set backlog_sweep_disabled false`. Mirrors `t3:scanning-news`'s cadence/ask-gate wiring; the queued task carries an `ASK-GATE` directive so the dispatched skill never mass-closes unattended. |
| `backlog_sweep_skill` | Override which skill the backlog-sweep scanner dispatches (default `backlog-sweep`) |
| `backlog_sweep_cadence_hours` | Cadence floor for the backlog-sweep scanner (default 168 = weekly) |
| `ask_before_backlog_sweep_closes` | Ask-gate for backlog-sweep issue closes (default `true`). When on, the dispatched skill records each close proposal with its citation and surfaces the batch for explicit approval instead of mass-closing — only the high-confidence merged-PR-superseded class auto-closes. Per-overlay overridable. |
| `max_concurrent_local_stacks` | #1397: cap on concurrent locally-running stacks per overlay (0 = unbounded). A heavy overlay caps to `1` while a cheap dogfood overlay stays unbounded; enforced by `t3 <overlay> worktree start` / `workspace start` |
| `provision_step_timeout_seconds` | #2220: hard ceiling (seconds) for one long-blocking provisioning subprocess — a DSLR snapshot restore, `migrate`, or a `--create-db` test-DB rebuild (default `1800`). On exceeding it the step ABORTS and fires a loud out-of-band user alert instead of grinding silently; a forked migration graph is diagnosed by its symptom immediately. A non-positive value degrades to the default (the "never hang" invariant cannot be configured away). Per-overlay overridable; enforced by `teatree.core.provision_timebox`. |
| `stale_stack_min_age_minutes` | #2207: stale-stack reaper threshold (minutes, default `0` = disabled, opt-in like `max_concurrent_local_stacks`; set e.g. `240` to enable). A docker compose stack with NO live `Worktree` row (a hand-rolled test stack, a failed-teardown leftover) is torn down once its newest container lifecycle event is older than this — automatically before `worktree start` / `workspace start` / `workspace provision`, and on demand via `t3 <overlay> workspace reap-stale [--dry-run]`. Age-keyed + fail-safe (unknown age ⇒ keep) so a parallel session's fresh manual stack is never reaped; `clean-all` remains the blunt every-unowned-project clean. Per-overlay overridable. |
| `orchestrator_bash_gate_enabled` | #115: kill-switch (default `true`) for the §17.6.4 gate 2 (`handle_enforce_orchestrator_boundary`). When on, the MAIN agent is blocked from running a LONG / HEAVY foreground `Bash` command (test suite, build, dev server, long sleep, full-tree sweep); `run_in_background: true` is the escape hatch, sub-agents unrestricted. Set `false` under `[teatree]` (read directly by the hook layer) or per-overlay to disable it — e.g. as the failsafe after `t3 update` reinstalls the gate. |
| `orchestrator_turn_budget` | Soft per-turn tool-call **count** budget (default `25`; `0` disables) for the §17.6.4 gate 2 responsiveness nudge (`handle_orchestrator_turn_budget_nudge`). Governs long TURNS (vs the heavy-`Bash` arm's long OPERATIONS) — once a MAIN-agent turn makes this many NON-orchestration tool calls, a one-time `additionalContext` line steers it to yield. Advisory only (never a deny); orchestration calls and sub-agents exempt. |
| `orchestrator_turn_wall_clock_seconds` | #1733 §2: the **wall-clock** dimension of the same responsiveness nudge (default `180`; `0` disables). Independent of `orchestrator_turn_budget` — once a MAIN-agent turn has run more than this many seconds of wall-clock since it started (the last user-visible action), the same one-time yield nudge fires even when few tool calls were made (the slow-but-few-calls case the count dimension misses). Both dimensions share one per-turn idempotent marker; advisory only; orchestration calls and sub-agents exempt. |
| `skill_loading_gate_enabled` | #1488: kill-switch (default `true`) for the §17.6.4 skill-loading gate that blocks `Bash`/`Edit`/`Write` and the fanned-out `TaskCreated` counterpart until the resolvable pending teatree skills load. Read directly by the hook layer; set `false` under `[teatree]` or per-overlay, or disable via `t3 <overlay> gate skill-loading disable`. |
| `plan_edit_gate_enabled` | Kill-switch (default `true`) for the §17.6.4 gate 16 early DX signal (`handle_block_edit_before_planned`) that denies `Edit`/`Write` while the worktree's ticket is in `STARTED` state. Read directly by the hook layer; set `false` under `[teatree]` or disable via `t3 <overlay> gate plan disable`. Per-call escape: `[skip-plan-gate: <reason>]` in `new_string`/`content`/`file_path` (first 512 chars). |
| `mcp_privacy_gate_enabled` | #171: canary off-switch (default `true`) for the Slack-MCP arm of the #1213 quote-scanner and #1218 bare-reference publish-privacy gates (reachable via the `mcp__.*[Ss]lack.*` matcher). Fails OPEN; set `false` to disable the Slack-MCP arm alone if it misfires. The Bash arm of both gates is unaffected. |
| `dispatch_quote_gate_on_task_create_enabled` | #171: opt-in switch (default `false`) for the `TaskCreated` dispatch-quote gate (`handle_dispatch_prompt_quote_scanner_on_task_create`) — the fan-out counterpart of the `PreToolUse` dispatch-quote gate (the `Task`/`Workflow` fan-out bypasses `PreToolUse`, so only `TaskCreated` reaches a fanned-out dispatch). Fails CLOSED (unvalidated fan-out gate stays inert by default); set `true` to scan fanned-out task subjects/descriptions for HIGH verbatim user quotes. Clears on a `[quote-ok: <reason>]` token. |
| `orchestrator_boundary_agent_gate_enabled` | Kill-switch (default `true`, #1733) for the `Agent` arm of the §17.6.4 gate 2 (`handle_enforce_orchestrator_boundary` → `_deny_foreground_agent_dispatch`, #1442), denying a main-agent FOREGROUND `Agent` dispatch. Now LIVE: an `Agent` `PreToolUse` matcher is wired in `hooks.json` ([#1646](https://github.com/souliane/teatree/issues/1646)) and the gate is default-ON after its attended pre-INSTALL dry-run. Fails OPEN to enabled on a missing/broken config; only an explicit bare `false` disables it. Off-ramps (never-lockout): sub-agent context, `run_in_background: true`, per-call `[fg-ok: <reason>]`, the deny-circuit-breaker, and — via `_fail_open_or_deny` ([#1692](https://github.com/souliane/teatree/issues/1692)) — the self-rescue allowlist + the master `danger_gate_fail_open` switch. The `Bash` arm (`orchestrator_bash_gate_enabled`) is unaffected. |
| `danger_gate_fail_open` | NEVER-LOCKOUT switch (default `false`): `true` flips every over-deny gate to fail-open. PUBLIC-egress gate excluded. The `danger_` prefix flags that a forgotten `true` override silently disables protective gates. See BLUEPRINT §17 invariant 10. |
| `mr_title_regex` | #1540: MR title pattern the `pr create` gate enforces (default Conventional Commits); an overlay declares its own grammar. The gate also requires a What/Why description, no bypass. |
| `private_repos` | Offline slug-NAMESPACE allowlist of known-private repos: each entry is a path-segment prefix of a host-stripped `owner/repo` slug (`acme-engineering` covers `acme-engineering/*`, host-qualified or bare), NOT a raw substring (#1953 — a substring match falsely downgraded a public SSH-alias remote). Drives the #126/#1657 carve-out and (unioned with `internal_publish_namespaces`, #1672) the destination skip, so a user with only this set needs no second list. `teatree.hooks._repo_visibility`. |
| `internal_publish_namespaces` | Destination allowlist (default `[]`) making the #1415/#1530 publish gates destination-aware: a target that prefix-matches is internal and skipped. #1672 unions it with `private_repos`, deciding the skip PER top-level segment — a chained/substituted public post or a raw-REST `api` segment forces the whole command SCANNED. FAIL-CLOSED (empty/unresolvable stay PUBLIC). `teatree.hooks.publish_destination`; env `T3_INTERNAL_PUBLISH_NAMESPACES` supplements. |
| `owned_repos` | The repo SCOPE axis (orthogonal to `private_repos`/visibility and to `author_is_self`/collaboration): a forge-host-keyed dict `{normalized-host: [namespace-pattern, …]}` of the repos this overlay legitimately works on (`{"github.com": ["souliane", "acme-eng/widget-overlay"]}`). Host equality is matched EXACTLY before the namespace half (`slug_namespace_matches`), so a `gitlab.com` repo never matches a `github.com` scope. A `[overlays.<name>.owned_repos]` TOML table REPLACES the settings dict (authoritative-and-complete, no deep-merge). Sole-element `["*"]` is a whole-host wildcard (self-hosted forges only). `teatree.core.repo_scope`. |
| `require_owned_repo_approval` | Opt-in (default `false`, ships INERT) for the unknown-repo gate (`teatree.core.gates.owned_repo_guard`): when `true` AND `owned_repos` non-empty, a push/merge to a repo no overlay owns is HELD for the operator. Fails CLOSED on a clean unknown verdict (opposite polarity to the visibility gate); enabling it therefore requires FIRST declaring the FULL owned host/namespace list (every private/customer forge the operator merges on) — a partial list would hold the operator's own private-forge merges as unknown. A **path-only** TOML overlay (`path` but no `class`) is skipped by `get_all_overlays` and cannot opt itself in; its repos go under an instantiable overlay's `owned_repos`. Opt in from private `~/.teatree.toml` (where brand/customer strings are allowed). Fails OPEN on a resolver exception / unresolvable host. Never-lockout: `[scope-push-ok: <reason>]` token + `[teatree] unknown_repo_push_gate_enabled = false` kill-switch. |
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

**Away-gate.** When availability resolves to `away` (§5.6.3), `resolve_speak()`
forces `local` to `off` while preserving the configured `slack` value — so no
audio plays through the local speakers while the user is unreachable, but a
Slack-attached rendition still reaches their phone. The user's
`[teatree.speak]` config is never mutated; the gate is purely effective-value.
Both local consumers (`speak()` and the local leg of `deliver_user_dm`)
resolve through `resolve_speak()`, so this single chokepoint silences all local
playback when away. The away check is exception-safe — a resolution failure is
treated as **not** away (local plays), so it can never spuriously mute audio or
turn `slack` off.

**Cross-process speaker mutual exclusion (#2152, bounded #2156).** Local
playback fans out from two independent sources — each DM's local-read leg and
the detached `t3 speak` Stop-hook read — each spawning its own `say`. To stop
concurrent reads talking over each other, `_speak_local()` wraps the actual
`say` call in a single cross-process `fcntl.flock` on a lockfile under the
teatree state dir (`get_data_dir("speak")/speaker.lock`), guaranteeing **mutual
exclusion** (no two `say` calls overlap) across both the in-process daemon
threads and the separate detached subprocesses (a per-process queue would not
serialize the subprocesses). The lock is acquired with a **bounded wait**, not
blocking: `_serial_speaker()` retries a non-blocking acquire for a short total
budget (`_SPEAKER_LOCK_WAIT_BUDGET_S`) and, if the speaker is still busy,
**drops the read as stale** rather than queuing it. A blocking acquire is not
FIFO and builds an unbounded backlog under a flood of fan-out reads — a message
could play many minutes after it was printed — so the bounded-wait-then-drop
caps latency at the budget: a read either plays promptly or is dropped, never
multi-minute late. The non-blocking daemon-thread dispatch is unchanged — the
thread waits on the lock, never the caller's egress path — so the bounded wait
never delays a DM or turn. The lock is best-effort: a lockfile that cannot be
opened fails **open** (the read still plays) so a lock error never mutes audio.

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

### 10.1.2 Agent model tiering & session pins (`[agent]`)

The `[agent]` table holds the model/effort settings for spawned sub-agents and
the interactive main agent. It is read with a raw `tomllib` parse
(`config_agent.resolve_agent_config` + `model_tiering._load_phase_model_overrides`),
independent of the per-overlay `[teatree]` merge — these are session-scoped
spawn inputs, not overridable `UserSettings`.

```toml
[agent]
session_model = "fable"           # interactive main-agent --model pin (so you never run /model by hand)
session_effort = "xhigh"          # interactive main-agent --effort pin (strict CLI scale)
# fable_enabled = false           # the single Fable kill-switch: flip to false to revert EVERY
                                  # Fable pin to the Opus 4.8 baseline (default true == keep Fable)
# fable_fallback = "opus"         # the model Fable downgrades to when disabled (default "opus" = Opus 4.8)

[agent.phase_models]              # per-PHASE model tier for the spawned sub-agent (model_tiering)
planning = "fable"                # pin a phase up; "" / "default" / "inherit" opts out
reviewing = "sonnet"
testing = ""                      # explicit inherit (no --model)

[agent.skill_models]              # per-COMPANION-SKILL model floor (MODEL only, no effort axis)
code-review = "opus"              # a loaded skill RAISES the spawn model to at least this tier
architecture-design = "fable"
```

**`session_model` / `session_effort` (interactive main agent only).** Injected
as `--model` / `--effort` into the interactive `claude` spawn argv by
`t3 loop start` (`cli/loop.py`'s `os.execv`), so the main agent runs at the
pinned model/effort without a manual `/model`. **Effort is settable only
session-wide** — never per-sub-agent (the Agent tool has no effort param) and
never on `claude -p` headless (`--model` only). The effort scale is the strict
CLI scale `low | medium | high | xhigh | max` (`max` > `xhigh`; there is **no**
`off`); an off-scale value is a hard `ValueError` at parse and a `t3 doctor`
FAIL. "ultracode" (xhigh + auto dynamic workflows) is a session/settings
concept, not a value here. A model sentinel (`""` / `"default"` / `"inherit"`)
means inherit the default (no flag).

**`[agent.phase_models]` (per-phase sub-agent tier).** The shipped default pins
`planning → opus` and downgrades mechanical phases (`reviewing`/`requesting_review`/`testing`/`shipping`
→ `sonnet`, `retrospecting` → `haiku`); reasoning phases (`coding`, `debugging`)
inherit. Override any phase here; a sentinel opts it out.

**`[agent.skill_models]` (per-companion-skill MODEL floor).** Maps a companion
skill name to a model floor. When a dispatch loads that skill,
`model_tiering.resolve_spawn_model` raises the spawn model to the most capable
of the phase tier and every loaded skill's floor (most-capable-wins via
`cost.tier_rank`, capability order `haiku < sonnet < opus < fable`; a floor only
RAISES, never downgrades). **MODEL only** — there is deliberately no per-skill
effort axis. With this table absent the spawn model is byte-for-byte the
per-phase tier. On an *inheriting* phase (`coding`/`debugging`, whose phase tier
is `None`) a floor raises the spawn model only when it is *strictly stronger*
than the assumed-opus inherited default — `tier_rank(None)` equals
`tier_rank("opus")`, so an `opus`-or-weaker floor is silently dropped (the phase
still inherits) and only a `fable` floor pins it up. `t3 doctor` WARNs on a floor
that names no known tier (likely a typo) since an unknown id ranks most-capable.

**`fable_enabled` / `fable_fallback` (the single Fable kill-switch, teatree#2237).**
Fable can be wired through several independent pins above (`session_model`, any
`phase_models.<phase>`, any `skill_models.<skill>`). If Fable becomes
unavailable, reverting to the Opus 4.8 baseline is **one flip**:
`fable_enabled = false`. With it off, every resolved model value that is Fable —
recognised by tier (`cost.tier_of_model`), so both the short alias `fable` and
the full id `claude-fable-5` match — transparently downgrades to `fable_fallback`
at the single resolution chokepoint (`model_tiering._downgrade_fable`, applied at
the end of `resolve_spawn_model` covering every sub-agent spawn, plus the
`session_model` `--model` pin in `cli/loop.py`). `fable_fallback` defaults to
`"opus"` (the tier/cost machinery maps it to `claude-opus-4-8`), so Opus 4.8
compatibility is preserved by construction. **The default is enabled** — an
absent `fable_enabled` key counts as `true`, so existing configs that pin Fable
keep resolving to Fable, byte-for-byte unchanged; only the explicit
`fable_enabled = false` flips the revert. Non-Fable pins (`sonnet`, `haiku`,
`opus`) and inheriting phases (`None`) pass through untouched either way.

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

**[#1775](https://github.com/souliane/teatree/issues/1775) — moving overridable config into the DB.** The `ConfigSetting` override tier (§10.1.1) deliberately lets the DB *own* user intent for an overridable setting, rather than only cache derived state. It stays inside this rule's spirit on two counts: (1) the DB is a strictly higher tier than the file — an empty table resolves byte-identically to today and the read fails safe to no-override when the DB is absent, so deleting the DB never loses the file-authored intent that remains the floor; and (2) the round-trip affordance is preserved by the `t3 <overlay> config_setting set|clear|list` admin path. The genuinely bootstrap-readable settings (`DATABASE_URL` / data-dir / `DJANGO_SETTINGS_MODULE` / `private_repos`) remain text-only — they must resolve before Django, so they can never move to this tier.
