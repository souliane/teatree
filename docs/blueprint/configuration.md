# BLUEPRINT Appendix — Configuration

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §10. Consumer cross-references such as `BLUEPRINT §10.1` (the config store, slack-bot setup) resolve here.

## 10. Configuration

### 10.1 Configuration store

Every setting lives in the teatree DB — the `ConfigSetting` store — set with
`t3 <overlay> config_setting set <key> <json>` (`--overlay <name>` scopes a value
to one overlay; omit it for the global default). There is no config file to edit.
The overlay-definition registry (`overlays`) and the external-E2E registry
(`e2e_repos`) are DB-home too — each is one JSON-dict `ConfigSetting` row that
`loader._inject_db_registries` injects into `config.raw`, so teatree boots fully
from the DB. `config_setting export` dumps the store to a TOML backup for a
round-trip interchange.

The keys and value shapes below are illustrative — set each one with
`config_setting set`; the `[table]` syntax only shows how a key is scoped
(`[teatree]` = global, `[overlays.<name>]` = that overlay):

```toml
[teatree]
# workspace_dir is DB-home now (per-overlay; default ~/workspace/t3-workspaces/<overlay>/).
# Set it with `t3 <overlay> config_setting set workspace_dir <path> [--overlay <name>]`;
# a value left here is ignored on read. T3_WORKSPACE_DIR env still overrides (back-compat).
privacy = "strict"
orchestrator_bash_gate_enabled = true      # #115 kill-switch, read directly by the hook layer (pre-Django, DB-first w/ TOML self-rescue)
# statusline_chain is DB-home now (extra statusline scripts, glob patterns, chained after the loop's zones).
# Set it with `t3 <overlay> config_setting set statusline_chain '[...]'`; a value left here is ignored on read (the bash hook reads the DB via the sqlite3 CLI).
# autoload is DB-home now (#256 default-OFF teatree engagement; true = auto-engage every session).
# Set it with `t3 <overlay> config_setting set autoload true`; a value left here is ignored on read. T3_AUTOLOAD env still overrides (cold-hook read).

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

# Cross-repo "my open MRs" Slack reminder (`t3 <overlay> mr_reminder`) is DB-home
# (config-unify). Routes each open MR/PR to a channel by repo slug
# (most-specific wins; an org-namespace prefix like "acme-engineering" routes its repos).
# A `[mr_reminder]` table left here is ignored on read; set it as a JSON dict:
#   t3 <overlay> config_setting set mr_reminder \
#     '{"default_channel":"C_FALLBACK","channels":{"souliane/teatree":"C_TEATREE","acme-engineering":"C_ACME"}}'
# Likewise `speak` (#2060) is DB-home: t3 <overlay> config_setting set speak '{"local":"all","slack":true}'
#   (per-overlay override: add --overlay <name>; it MERGES onto the global speak row).

```

The operational (DB-home) settings are set in the store —
globally, or scoped to one overlay with `--overlay <name>`:

```bash
t3 <overlay> config_setting set mode auto                                  # global default
t3 <overlay> config_setting set require_human_approval_to_merge false --overlay myproject
t3 <overlay> config_setting set on_behalf_post_mode immediate
t3 <overlay> config_setting set user_identity_aliases '["handle-a", "handle-b"]'
t3 <overlay> config_setting export [--overlay myproject] [--output dump.toml]  # dump the store to a TOML backup (stdout default)
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
assembly + routing are pure (`teatree.core.review.mr_reminder`); the per-channel post routes
through the on-behalf egress chokepoint (`OnBehalfSlackEgress.post`), so a reminder
channel (a colleague surface) is gated + audited like any on-behalf post.

**Slack bot setup** (`t3 setup slack-bot --overlay <name>`): an interactive walkthrough scaffolds the per-overlay Slack app and stores its tokens. Steps:

1. Print the manifest JSON (with `messages_tab_enabled`, `app_mentions:read` scope, Socket Mode, bot events `app_mention` + `message.im`) and open the Slack app creation page. The user pastes the manifest, creates the app, installs it to the workspace, and generates an app-level token with `connections:write` scope.
2. Capture the bot token (`xoxb-…`) and app-level token (`xapp-…`) into `pass` entries `<slack_token_ref>-bot` and `<slack_token_ref>-app`.
3. Auto-detect the user's Slack ID from `git config user.email` via the Slack API. Falls back to a manual prompt when detection fails.
4. Store `messaging_backend`, `slack_user_id`, and `slack_token_ref` in the `overlays` registry row for `<name>` in the DB `ConfigSetting` store.
5. Smoke-test by sending a DM via the bot and waiting for the user to react with ✅.

The walkthrough never writes a bot token to disk in plaintext; tokens always go via `pass`. Re-running `t3 setup slack-bot --overlay <name> --reset` rotates both tokens but **skips the manifest** — it does **not** apply a scope change.

For an **existing** app, the command updates its manifest in place. When `[overlays.<name>].slack_app_id` is recorded (the create flow prompts and persists it) — or `--update` is passed (prompting for the app id when none is recorded) — it calls Slack's `apps.manifest.export` / `apps.manifest.update` using org-wide config tokens in `pass` (`teatree/slack-app-config-token`, `teatree/slack-app-config-refresh`; auto-rotated via `tooling.tokens.rotate` on `invalid_auth`/`token_expired`). A matching manifest is an idempotent no-op; otherwise it is applied and the **single** remaining manual step is the browser OAuth-consent reinstall click at the deep link (`https://api.slack.com/apps/<app_id>/install-on-team`). With no config token stored it degrades: prints the manifest plus the manifest-editor deep link for a manual paste, then smoke-tests with the stored bot token. Adding/changing a manifest scope (e.g. granting the xoxp user token `reactions:write`) requires a full reinstall via this path so Slack re-prompts OAuth consent for the new scope set.

**One-command full setup** (`t3 setup slack-provision [--overlay <name>]`): runs the entire Slack lifecycle for one overlay — or every `messaging_backend = "slack"` overlay when `--overlay` is omitted — in one idempotent pass, replacing the `slack-bot` + `slack-user-token` + manual-channel-invite sequence ([#1686](https://github.com/souliane/teatree/issues/1686)). Per overlay it: resolves the app id (config → derive from bot token → prompt, persisted via the shared `slack_app_resolve` helper that also stops `slack-bot --update` prompting); pushes the manifest (all bot + user scopes incl. `reactions:write`) via `apps.manifest.update`; prints + opens the OAuth (re)install URL (the one manual step — `--no-open-browser` suppresses); joins the bot to its review-broadcast channels via `conversations.join` so its first post/reaction does not fail `not_in_channel` (private/Connect channels print a manual `/invite` line); provisions the bot IM channel; and verifies the shared xoxp token carries every required scope. Never deletes credentials; safe to re-run.

**Scope profiles.** An overlay's `slack_scope_profile` (DB `overlays` registry row) selects how much Slack reach its bot gets — set it with `t3 setup slack-provision --overlay <name> --dm-only` (or `--full` to revert; the flag persists the profile before provisioning, and is refused without `--overlay`). The default `"full"` provisions the read/write-everywhere bot plus the shared xoxp user token — the profile customer overlays use to post across channels and Slack-Connect. `"dm_only"` provisions a bot that may talk ONLY to its one owner's DM: `slack-provision` pushes a minimal bot-scope manifest (`chat:write`, `im:*`, `reactions:*`, `users:read`, `files:write`) with **no** `user` (xoxp) scope section, joins no review channels, and skips the user-token step; the socket doctor validates it against a DM-only requirement set so it is never re-widened; and the loader builds its `SlackBotBackend` with `owner_dm_only=True`, which raises `OwnerDmOnlyError` at the token funnels on any outbound to a channel or user that is not the owner's self-DM. This is the profile teatree's own `t3-teatree` overlay uses so its bot is a private 1:1 with its human, not a workspace-wide agent.

**Socket Mode listener** (`t3 slack listen`): a global singleton process that opens one WebSocket per slack-enabled overlay. Events are written to `$XDG_DATA_HOME/teatree/slack-events.jsonl` in real time. `t3 slack status` checks if the listener is running. `t3 slack check` drains the queue and prints user messages as JSON (exit 0 = messages found, 1 = empty) — designed for a fast cron (30s–1min). The listener uses the shared `teatree.utils.singleton` flock primitive (kernel-enforced, crash-safe) — only one instance runs at a time. Start it as a background process or let the SessionStart hook manage its lifecycle.

**Operating mode (DB-home `mode`, set via `t3 <overlay> config_setting set mode …`;
env: `T3_MODE`)** — controls whether the agent
pauses for confirmation on publishing actions (push, PR create, PR merge, messaging-backend
posts, remote branch deletion):

| Mode | Default | Meaning |
|------|---------|---------|
| `interactive` | ✅ | Canonical default. Confirm before push, PR create, messaging-backend posts, any remote write. Always-gated destructive ops (force-push to default branches, history rewrites on shared defaults, destructive DB ops on non-ticket schemas, unauthorized external writes) stay gated regardless of mode. |
| `auto` |  | Opt-in per overlay. End-to-end autonomy: push, PR create, clean-all's branch pruning, retro writes, overlay-approved messaging-backend posts run without prompts. Merge is gated by `require_human_approval_to_merge` (default `true`). Always-gated destructive ops still apply. Recommended for personal dogfooding overlays where the user accepts the trust boundary; use `interactive` for client / shared-team overlays. |

The env var `T3_MODE` overrides the stored DB-home value. Unknown values raise
`ValueError` — typos never silently downgrade to a less-safe mode.

### 10.1.1 Per-Overlay Setting Overrides

**Setting-home partition ([#1775](https://github.com/souliane/teatree/issues/1775)).**
Every non-derived `UserSettings` field has EXACTLY ONE home, declared in the
typed registry `config/homes.py` (`SettingHome` ∈ {`DB`, `TOML`}, `SETTING_HOMES`):
a field that CAN live in the DB is **DB-home**. As of config-unify
the carve-out is **EMPTY** — every `UserSettings` field is DB-home. The two homes are
disjoint (a fitness function asserts it) — a setting is never read from both tiers. A
DB-home field resolves from `ConfigSetting` (global + overlay rows) + env only.
(config-unify moved `check_updates` — its pre-Django reader
`check_for_updates` now reads the DB via the Django-free `cold_reader` — `timezone`
— the Django settings module hardcodes `TIME_ZONE` and configures
`DATABASES` without reading it, so it was not a bootstrap dep (its former sibling
`worktrees_dir` was removed as a redundant duplicate of `worktree_root()`) — the two former
per-overlay-TOML-overridable fields `orchestrator_bash_gate_enabled` / `privacy` —
per-overlay override now lives in a `ConfigSetting` overlay-scope row — `handover_mirror_path`
— its pre-Django SessionStart reader reads the DB via `cold_reader`, which fails open to
the same default bootstrap path `write_mirror` uses when unset; that default is the SHARED
data dir (`$T3_DATA_DIR` when set, else `${XDG_DATA_HOME:-~/.local/share}/teatree`) plus
`handover/latest.md` (#3563) — the state dir is runtime-local, so a hand-off created inside
the worker container wrote its mirror to a filesystem the host could not read and the host's
`latest.md` stayed pinned to an ancient session, while the data dir is the one directory
every runtime shares (the deploy already bind-mounts it) — `statusline_chain` —
the bash statusline hook reads it from the canonical sqlite via the `sqlite3` CLI +
`json_each` — `statusline_engaged_render` — the #3502 opt-in (strict bool, default OFF)
that renders the statusline in a hand-engaged session (an engage marker present) even
with autoload off, read DB-only by the bash statusline
(`statusline.sh._statusline_engaged_render_db_value`) — and `autoload` — the #256 engagement flag; its cold SessionStart /
UserPromptSubmit readers (`teatree_settings.autoload_enabled` via `_cold_db_bool`,
`statusline.sh` via the `sqlite3` CLI) read the DB ONLY, so a `[teatree] autoload`
value is ignored on read and the how-to advisory points at
`config_setting set autoload true` — and finally the last two, the nested structured
tables `speak` / `mr_reminder` — stored as JSON-dict `ConfigSetting` rows
(`parse_speak_setting` / `parse_mr_reminder_setting`) and rebuilt bespoke by the
resolver (`resolution._BESPOKE_STRUCTURED_FIELDS`), with `speak` keeping its
per-overlay MERGE; the cold Stop-hook `speak` reader uses `cold_reader.read_setting`
— to the DB. The last NON-`UserSettings` config — the `[overlays]` overlay-definition
registry and the `[e2e_repos]` registry — is DB-home too: each is stored as one JSON-dict
`ConfigSetting` row (`config/registries.py`, `REGISTRY_SETTINGS`) and
`loader._inject_db_registries` overrides `config.raw["overlays"]` / `config.raw["e2e_repos"]`
from the store via the Django-free `cold_reader`, so every existing reader
(`discover_overlays`, `load_e2e_repos`) is untouched and teatree boots fully DB-configured
with no config file present. Because the DB row REPLACES
the whole file table, a lingering `[overlays.<name>]` / `[e2e_repos.<name>]` value that
DIVERGES from the DB row would be silently masked on read — editing an overlay `path` in the
file had NO effect and returned the stale DB value with zero signal
([#128](https://github.com/souliane/teatree/issues/128)). `load_config` now surfaces that
mask: it names each masked leaf and both reconcile commands as a loud WARN by default, RAISES
`RegistryTomlMaskError` under `enforce_registry_partition=True`, and `t3 doctor`'s
`_check_registry_toml_drift` hard-FAILs on it. The remediation actually works because
`config_setting import` reads the RAW file (`load_raw_toml`), not the DB-injected `config.raw`
— so an edited `path` is migrated instead of the stale DB row re-written.)
`workspace_dir` is **DB-home** and
per-overlay overridable (it is read only after Django is up): it names the
per-overlay **WORKTREE root** where ticket worktrees are created — worktrees
regroup under a per-overlay default `~/workspace/t3-workspaces/<overlay>/`,
resolved by `config.worktree_root()` — `T3_WORKSPACE_DIR` env/Django-setting
override (highest, back-compat) → DB `ConfigSetting` (overlay scope, then global)
→ that default. This is **distinct from** the **CLONE root** `config.clone_root()`
(`~/workspace`, where main repo clones live; `T3_WORKSPACE_DIR` env/Django-setting
override → `~/workspace`), which `find_clone_path` and every clone-discovery caller
use — conflating the two would make provisioning scan the worktree root for clones
and fail. A `[teatree] workspace_dir` (or `[overlays.<name>] workspace_dir`) value
left in TOML is **ignored on read** and warned about on load (it silently relocated
worktrees pre-warning); migrate it into the store with `t3 <overlay> config_setting
import` or set it explicitly. `t3 <overlay> workspace relocate` moves an overlay's
EXISTING teatree-managed worktrees to that per-overlay dir with `git worktree move`
(skipping any locked / dirty / live mid-task one, idempotent, `--dry-run`-able). The one DERIVED field
(`notify_on_behalf`) is computed by the resolver and has no home. The
resolution-tier wiring below details each home's chain.

The resolution chain is **per home** (first match wins) — each field reads from
exactly the tiers its home allows:

* **DB-home field** (every field not in the carve-out): `T3_*` env var (wired one-offs
  in `ENV_SETTING_OVERRIDES`: `T3_MODE`, `T3_WIP`, `T3_ON_BEHALF_POST_MODE`,
  `T3_MISSING_ISSUE_POLICY`, `T3_REVIEW_SKILL`), then the **DB store** — an
  **overlay-scoped** `ConfigSetting` row, then a **global** row — then, for a key
  **promoted to an overlay code default** (#36, `config.overlay_code_defaults`),
  the active overlay's `OverlayConfig` value, then the `UserSettings` dataclass
  default. Its `[teatree]` / `[overlays.<name>]` TOML value is **ignored on read**
  (left over from a pre-partition config, it is migrated into the store with
  `t3 <overlay> config_setting import`).
* **Overlay-code-default tier** (#36): a genuinely-constant, non-secret setting
  (e.g. `review_skill`, the `*_skill` scanner names, `mr_title_regex`) may live as
  a Python default on the active overlay's `OverlayConfig` (fed by its
  `overlay_settings.py`), still DB-overridable. That default sits **below every DB
  / env override** (a `ConfigSetting` row still wins) and **above the dataclass
  default** (with no row the overlay code default wins). It is a DEFAULT, never a
  hard pin — it never defeats the autonomy collapse. The tier is an inverted seam
  (`teatree.core` registers a provider on `config.overlay_code_defaults` at
  overlay-load time, mirroring `command_catalogue`); with no provider registered
  it is a strict no-op and resolution falls straight through to the dataclass
  default.
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
value for a DB-home key migrates both in one pass. `import` also seeds the two
NON-`UserSettings` registries: the `[overlays]` overlay-definition keys (the overlay's
own `path` / `class` / messaging keys — everything that is NOT a setting) into one global
`overlays` row, and `[e2e_repos]` into one global `e2e_repos` row. `set` / `get` admit
those two keys too (their parser registry is `REGISTRY_SETTINGS`, kept separate from
`OVERLAY_OVERRIDABLE_SETTINGS` so the resolver's per-field coercion never sees them). Run
it once after upgrading to the partition so an existing install's DB-home keys keep
applying (they are otherwise ignored on read).

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
`src/teatree/config/settings.py`, alongside the gate-flag and registry settings
`t3 <overlay> config_setting set` also accepts (`COLD_HOOK_SETTINGS` in
`config/cold_hook_settings.py`, `REGISTRY_SETTINGS` in `config/registries.py`).
The code registries are the single source of truth — the curated table below
explains why representative keys are per-overlay-overridable; consult the
registries for the full set, type signatures, and defaults.

| Key | Why overridable |
|-----|------------------|
| `mode` | `auto` for a personal dogfooding overlay, `interactive` for a client overlay |
| `autonomy` | Single trust switch, tiers `full > notify > babysit` (default `babysit`). Both autonomous tiers collapse the three approval gates (colleague auto-approve via `on_behalf_post_mode`, auto-merge, auto-answer) and pin `mode = auto`; `full` enables the single-author `solo_overlay` merge bypass, `notify` derives `notify_on_behalf = true` and keeps the colleague-approval CLEAR merge path. An explicit per-gate value wins, and a global `mode` does not defeat the `mode = auto` pin (a per-overlay one does). Set without hand-editing TOML via `t3 <overlay> autonomy set <tier>` (`--overlay <name>` / `--global`); `t3 <overlay> autonomy show` reports the effective tier. Safety floor untouched |
| `wip` | Bounded-WIP throughput dial `slow < medium < full < boost` (default `medium`): how much new work a tick admits at once, orthogonal to `mode`/`autonomy`. `t3 <overlay> wip set`; `T3_WIP` env. |
| `privacy` | Stricter for client code, looser for personal |
| `contribute` | Contribute to one overlay's skills but not another |
| `excluded_skills` | Project-specific skill exclusions |
| `loop_cadence_seconds` | Per-overlay tick cadence (e.g. tighter on a hot overlay, looser on a maintenance one) |
| `require_human_approval_to_merge` | Training-wheel: auto-mode overlay can publish autonomously, merge stays gated |
| `substrate_self_signoff` | Explicit, default-off opt-in (#3223) that lets the standing grant sign off a **substrate** merge (merge keystone, architecture spec, governance doc, self-guardrail seam) on an overlay standing at `autonomy = full` (the solo-owned tier). Default `false` preserves the #2727 safety posture — substrate PINGS-and-HOLDS for the owner even at `full`. Turning it on changes only WHO authorizes the sign-off; the quality/safety floor (independent cold review, reviewed-SHA bind, CI-green, not-draft, maker≠checker, anti-vacuity) still runs. The `full` tier gate is kept so a below-full overlay never self-merges substrate even with the setting on. DB-home, per-overlay overridable; wired through `_overlay_grants_standing_substrate_signoff` |
| `substrate_auto_merge_authorized_by` | Owner-id STANDING substrate delegation (#3413), default empty (`""`). Empty preserves the hold-for-owner posture verbatim — a substrate CLEAR PINGS-and-HOLDS and is never auto-merged (invariant 4). Set to an owner id, the config WRITE is the durable, revocable authorization: the headless `pr_sweep` re-presents that id at merge time as the `--human-authorized` a substrate CLEAR requires, and the keystone (`_config_standing_substrate_delegation`) authorizes ONLY when the presented id still equals this configured value — sourced from config, never a live flag, so unsetting it revokes the delegation at the next merge. Every gate still runs (green required checks, recorded merge_safe verdict, clean rebase, draft-lock, maker≠checker, SHA-bind); on each such auto-merge the owner gets an "informed, not asked" Slack DM, and the merge is audited as config-sourced (`MergeAudit.standing_delegation_by`), distinct from a per-PR recorded human approval. Orthogonal to `substrate_self_signoff` (a `full`-tier bool self-signoff): this is a specific-owner-id, autonomy-independent delegation. DB-home, per-overlay overridable |
| `require_human_approval_to_answer` | Training-wheel for `t3:answerer`: drafts + DMs, posts only on confirm |
| `on_behalf_post_mode` | Tri-state pre-gate (#960) over colleague-VISIBLE posts: `draft_or_ask` / `ask` / `immediate`, scoped per overlay so a client overlay can stay `ask` while a personal one runs `immediate`. Drafts (`post-draft-note`) are colleague-invisible and exempt under every mode — they never need approval |
| `missing_issue_ref_policy` | What to do when a commit/MR needs an issue ref and none is in hand: `find_existing_then_ask` (default) / `create` / `dummy`. Scoped per overlay so a colleague-facing client overlay stays on the never-create default while a personal one can opt into `create`. The default always recovers the original existing issue first, then ASKs on a colleague-facing repo and CREATEs on the user's own repo — never a dummy. `create` / `dummy` are opt-in and authorise auto-create / a placeholder ref on colleague repos too. Resolved by `teatree.missing_issue_policy.resolve_missing_issue_verdict`; `T3_MISSING_ISSUE_POLICY` env wins; agent prose in `skills/ship/SKILL.md` § 0a |
| `on_behalf_auto_actions` | Allowlist of on-behalf actions that PROCEED even under `ask`/`draft_or_ask` (default `["post_e2e_evidence"]`): the user's own-ticket self-documentation, not a colleague-facing voice, so they never need per-post approval. Clear to `[]` to re-gate test plan; env `T3_ON_BEHALF_AUTO_ACTIONS` (comma-separated) wins |
| `review_request_post_disabled` | Whether agent-driven review-request posting is BLOCKED for this overlay (#2579, replacing the deleted `agent_review_request_disabled` side flag). Resolved off the autonomy TIER by `_apply_autonomy`: the `notify` tier sets it `true` so `resolve_on_behalf_verdict("review_request_post")` BLOCKs **regardless of `on_behalf_post_mode`** (including the `immediate` value the collapse forces); the `full` tier leaves it `false` (review-request proceeds); `babysit` keeps the default `false` so review-request follows `on_behalf_post_mode`. The customer-overlay done-definition gate: an overlay running `notify` stops at "MR is mergeable + review-requestable" and never auto-requests review. An explicit per-overlay pin always wins (Option A — the per-overlay escape): a `full` overlay can pin `true` to suppress auto-request, a `notify` overlay can pin `false` to opt back in. Orthogonal to `require_human_approval_to_merge` (which gates merge, not the review-request post). Scoped per overlay |
| `notify_user_via_bot` | Whether the bot→operator `notify_user(...)` channel (#963) DMs the user via the overlay's Slack bot (out of scope for the on-behalf gates — see `config/settings.py` for the boundary) |
| `notify_on_post_on_behalf` | DM the user after every on-behalf post (#949) — per-overlay because noise tolerance differs |
| `admin_autologin_enabled` | Whether the loopback admin dashboard auto-logs-in the first superuser (`LocalAdminAutoLoginMiddleware`). Default `true` so `t3 admin` and the deploy's loopback admin need no password. Never opens the admin alone: the middleware ALSO requires a loopback source (`127.0.0.1` / `::1` / `INTERNAL_IPS`), so a non-loopback request is never auto-logged-in even with the flag on — decoupled from `DEBUG`. Set `false` to force Django's auth wall. Per-overlay overridable |
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
| `backlog_sweep_disabled` | Kill switch for the periodic backlog-sweep scanner (`backlog_sweep`, #2419) — **defaults `true` (default-OFF)** because the sweep is destructive-capable (it can propose closing issues). The loop fires a weekly `backlog_sweep` task only after the user opts in with `config_setting set backlog_sweep_disabled false`. Mirrors `t3:scanning-news`'s cadence/ask-gate wiring; the queued task carries an `ASK-GATE` directive so the dispatched skill never mass-closes or mass-folds unattended. |
| `backlog_sweep_skill` | Override which skill the backlog-sweep scanner dispatches (default `sweeping-tickets`) |
| `backlog_sweep_cadence_hours` | Cadence floor for the backlog-sweep scanner (default 168 = weekly) |
| `ask_before_backlog_sweep_closes` | Ask-gate for backlog-sweep issue closes (default `true`). When on, the dispatched skill records each close/fold proposal with its citation and surfaces the batch for explicit approval instead of mass-closing — only the high-confidence shipped-by-merged-PR class auto-closes. Per-overlay overridable. |
| `max_concurrent_local_stacks` | #1397: cap on concurrent locally-running stacks per overlay (default `1`, the headless-safe single in-flight stack; `0` = unbounded). A heavy overlay caps to `1` while a cheap dogfood overlay can relax to `0`; enforced by `t3 <overlay> worktree start` / `workspace start` |
| `provision_step_timeout_seconds` | #2220: hard ceiling (seconds) for one long-blocking (HEAVY) provisioning subprocess — a DSLR snapshot restore, `migrate`, or a `--create-db` test-DB rebuild (default `1800`). On exceeding it the step ABORTS and fires a loud out-of-band user alert instead of grinding silently; a forked migration graph is diagnosed by its symptom immediately. A non-positive value degrades to the default (the "never hang" invariant cannot be configured away). Only steps marked `ProvisionStep.heavy` consult this ceiling — see `provision_fast_step_timeout_seconds` for every other step. Per-overlay overridable; enforced by `teatree.core.provision.provision_timebox`. |
| `provision_fast_step_timeout_seconds` | #2949: hard ceiling (seconds) for a FAST provisioning step (symlinks, settings, a compose override) — default `120`. The uniform 1800s ceiling let two grinding fast steps burn an hour before failure surfaced; a step opts into the long ceiling via `ProvisionStep.heavy`. Per-overlay overridable; enforced by `teatree.core.provision.provision_timebox`. |
| `provision_max_concurrency` | #2949: concurrency cap for `workspace provision`'s bounded worktree-provision subprocess pool (default `0` = auto-derive from `os.cpu_count()` at each read, `teatree.utils.ram_probe.default_provision_concurrency`). A positive value pins an explicit cap. Per-overlay overridable. |
| `provision_ram_ceiling_percent` | #2949: RAM-used-percent ceiling above which a new worktree provision is HELD (queued, not started) rather than admitted (default `85`) — mirrors the self-improve budget gate's RAM guardrail. A held request drains automatically once RAM frees, bounded by an internal max-hold ceiling. Per-overlay overridable; enforced by `teatree.core.gates.provision_admission_gate`. |
| `provision_slow_threshold_seconds` | #2949: a provision whose total duration exceeds this many seconds (default `600`) fires the same best-effort out-of-band user alert a single step timeout does. Per-overlay overridable. |
| `snapshot_warmer_max_age_days` | #2949: a reference-DB DSLR snapshot older than this many days (default `1`) is STALE; the snapshot-warmer loop refreshes it out-of-band so a ticket-critical-path provision never pays the slow restore+migrate path. Per-overlay overridable. |
| `snapshot_warmer_disabled` | #2949: kill-switch (default `false`) for the snapshot-warmer loop scanner. |
| `stale_stack_min_age_minutes` | #2207: stale-stack reaper threshold (minutes, default `0` = disabled, opt-in; set e.g. `240` to enable). A teatree-provisioned compose stack (named `<repo>-wt<ticket-pk>`) with NO live `Worktree` row — a failed-teardown leftover, or a hand-rolled stack started inside a worktree, which inherits `COMPOSE_PROJECT_NAME` from the env cache — is torn down once its newest container lifecycle event is older than this: automatically before `worktree start` / `workspace start` / `workspace provision`, and on demand via `t3 <overlay> workspace reap-stale [--dry-run]`. Age-keyed + fail-safe (unknown age ⇒ keep) so a parallel session's fresh stack is never reaped, and ownership-gated so a stack teatree did not provision (the deploy stack, an unrelated user project) is never a candidate on either this path or `clean-all`. Per-overlay overridable. |
| `orchestrator_bash_gate_enabled` | #115: kill-switch (default `true`) for the §17.6.4 gate 2 (`handle_enforce_orchestrator_boundary`). When on, the MAIN agent is blocked from running a LONG / HEAVY foreground `Bash` command (test suite, build, dev server, long sleep, full-tree sweep); `run_in_background: true` is the escape hatch, sub-agents unrestricted. Set `false` under `[teatree]` (read directly by the hook layer) or per-overlay to disable it — e.g. as the failsafe after `t3 update` reinstalls the gate. |
| `orchestrator_turn_budget` | Soft per-turn tool-call **count** budget (default `25`; `0` disables) for the §17.6.4 gate 2 responsiveness nudge (`handle_orchestrator_turn_budget_nudge`). Governs long TURNS (vs the heavy-`Bash` arm's long OPERATIONS) — once a MAIN-agent turn makes this many NON-orchestration tool calls, a one-time `additionalContext` line steers it to yield. Advisory only (never a deny); orchestration calls and sub-agents exempt. |
| `orchestrator_turn_wall_clock_seconds` | #1733 §2: the **wall-clock** dimension of the same responsiveness nudge (default `180`; `0` disables). Independent of `orchestrator_turn_budget` — once a MAIN-agent turn has run more than this many seconds of wall-clock since it started (the last user-visible action), the same one-time yield nudge fires even when few tool calls were made (the slow-but-few-calls case the count dimension misses). Both dimensions share one per-turn idempotent marker; advisory only; orchestration calls and sub-agents exempt. |
| `skill_loading_gate_enabled` | #1488: kill-switch (default `true`) for the §17.6.4 skill-loading gate that blocks `Bash`/`Edit`/`Write` and the fanned-out `TaskCreated` counterpart until the resolvable pending teatree skills load. Read directly by the hook layer; set `false` under `[teatree]` or per-overlay, or disable via `t3 <overlay> gate skill-loading disable`. |
| `plan_edit_gate_enabled` | Kill-switch (default `true`) for the §17.6.4 gate 16 early DX signal (`handle_block_edit_before_planned`) that denies `Edit`/`Write` while the worktree's ticket is in `STARTED` state. Read directly by the hook layer; set `false` under `[teatree]` or disable via `t3 <overlay> gate plan disable`. Per-call escape: `[skip-plan-gate: <reason>]` in `new_string`/`content`/`file_path` (first 512 chars). |
| `gate_relaxation_gate_enabled` | Kill-switch (default `true`, [#850](https://github.com/souliane/teatree/issues/850)) for the §17.6.1/§17.6.2 anti-relaxation + tach-soundness prek gate (`scripts/hooks/check_gate_relaxation.py`) that refuses a commit whose staged diff relaxes a lint/coverage constraint or a tach boundary. DB-home (per-overlay overridable): the hook resolves it DB-first through `get_effective_settings`, so `config_setting set gate_relaxation_gate_enabled false` actuates it exactly like the sibling gates, and `t3 <overlay> gate gate-relaxation disable` is the self-rescue. Per-commit escape: the `ALLOW_GATE_RELAX='<reason>'` env marker (non-empty reason) records a sanctioned relaxation and lets the commit through. |
| `mcp_privacy_gate_enabled` | #171: canary off-switch (default `true`) for the Slack-MCP arm of the #1213 quote-scanner and #1218 bare-reference publish-privacy gates (reachable via the `mcp__.*[Ss]lack.*` matcher). Fails OPEN; set `false` to disable the Slack-MCP arm alone if it misfires. The Bash arm of both gates is unaffected. |
| `dispatch_quote_gate_on_task_create_enabled` | #171: opt-in switch (default `false`) for the `TaskCreated` dispatch-quote gate (`handle_dispatch_prompt_quote_scanner_on_task_create`) — the fan-out counterpart of the `PreToolUse` dispatch-quote gate (the `Task`/`Workflow` fan-out bypasses `PreToolUse`, so only `TaskCreated` reaches a fanned-out dispatch). Fails CLOSED (unvalidated fan-out gate stays inert by default); set `true` to scan fanned-out task subjects/descriptions for HIGH verbatim user quotes. Clears on a `[quote-ok: <reason>]` token. |
| `dispatch_quote_scan_enabled` | #1564: kill-switch (default `true`) for the `PreToolUse` pre-dispatch quote scan (`handle_dispatch_prompt_quote_scanner`) that refuses an `Agent`/`Task` dispatch whose prompt carries a HIGH verbatim user quote. Set `false` under `[teatree]` to disable the scan if it misfires. Fails OPEN to enabled; **fails LOUD on an unknown value** — a non-boolean (`"yes"`, `on`, `2`) emits one stderr `WARNING` line and keeps the protective default rather than silently swallowing the misconfiguration (`teatree_bool_setting_loud`). |
| `orchestrator_boundary_agent_gate_enabled` | Kill-switch (default `true`, #1733) for the `Agent` arm of the §17.6.4 gate 2 (`handle_enforce_orchestrator_boundary` → `_deny_foreground_agent_dispatch`, #1442), denying a main-agent FOREGROUND `Agent` dispatch. Now LIVE: an `Agent` `PreToolUse` matcher is wired in `hooks.json` ([#1646](https://github.com/souliane/teatree/issues/1646)) and the gate is default-ON after its attended pre-INSTALL dry-run. Fails OPEN to enabled on a missing/broken config; only an explicit bare `false` disables it. Off-ramps (never-lockout): sub-agent context, `run_in_background: true`, per-call `[fg-ok: <reason>]`, the deny-circuit-breaker, and — via `_fail_open_or_deny` ([#1692](https://github.com/souliane/teatree/issues/1692)) — the self-rescue allowlist + the master `danger_gate_fail_open` switch. The `Bash` arm (`orchestrator_bash_gate_enabled`) is unaffected. |
| `danger_gate_fail_open` | NEVER-LOCKOUT switch (default `false`): `true` flips every over-deny gate to fail-open. PUBLIC-egress gate excluded. The `danger_` prefix flags that a forgotten `true` override silently disables protective gates. See BLUEPRINT §17 invariant 10. |
| `mr_title_regex` | #1540: MR title pattern the `pr create` gate enforces (default Conventional Commits); an overlay declares its own grammar. The gate also requires a What/Why description, no bypass. |
| `private_repos` | Offline slug-NAMESPACE allowlist of known-private repos: each entry is a path-segment prefix of a host-stripped `owner/repo` slug (`acme-engineering` covers `acme-engineering/*`, host-qualified or bare), NOT a raw substring (#1953 — a substring match falsely downgraded a public SSH-alias remote). Drives the #126/#1657 carve-out and (unioned with `internal_publish_namespaces`, #1672) the destination skip, so a user with only this set needs no second list. `teatree.hooks._repo_visibility`. |
| `internal_publish_namespaces` | Destination allowlist (default `[]`) making the #1415/#1530 publish gates destination-aware: a target that prefix-matches is internal and skipped. #1672 unions it with `private_repos`, deciding the skip PER top-level segment — a chained/substituted public post or a raw-REST `api` segment forces the whole command SCANNED. FAIL-CLOSED (empty/unresolvable stay PUBLIC). `teatree.hooks.publish_destination`; env `T3_INTERNAL_PUBLISH_NAMESPACES` supplements. |
| `owned_repos` | The repo SCOPE axis (orthogonal to `private_repos`/visibility and to `author_is_self`/collaboration): a forge-host-keyed dict `{normalized-host: [namespace-pattern, …]}` of the repos this overlay legitimately works on (`{"github.com": ["souliane", "acme-eng/widget-overlay"]}`). Host equality is matched EXACTLY before the namespace half (`slug_namespace_matches`), so a `gitlab.com` repo never matches a `github.com` scope. A `[overlays.<name>.owned_repos]` TOML table REPLACES the settings dict (authoritative-and-complete, no deep-merge). Sole-element `["*"]` is a whole-host wildcard (self-hosted forges only). `teatree.core.intake.repo_scope`. |
| `require_owned_repo_approval` | Opt-in (default `false`, ships INERT) for the unknown-repo gate (`teatree.core.gates.owned_repo_guard`): when `true` AND `owned_repos` non-empty, a push/merge to a repo no overlay owns is HELD for the operator. Fails CLOSED on a clean unknown verdict (opposite polarity to the visibility gate); enabling it therefore requires FIRST declaring the FULL owned host/namespace list (every private/customer forge the operator merges on) — a partial list would hold the operator's own private-forge merges as unknown. A **path-only** overlay (`path` but no `class`) is skipped by `get_all_overlays` and cannot opt itself in; its repos go under an instantiable overlay's `owned_repos`. Opt in from the private DB `ConfigSetting` store (where brand/customer strings are allowed). Fails OPEN on a resolver exception / unresolvable host. Never-lockout: `[scope-push-ok: <reason>]` token + `[teatree] unknown_repo_push_gate_enabled = false` kill-switch. |
| `speak` | #2060: text-to-speech config — `local` enum (`off`/`dm`/`all`) + `slack` bool, plus the #2171 meeting-mute opt-in `presence_backend` (`""`/`msteams`) + its `presence_token_ref` (`pass` entry). DB-home (#1775): a JSON-dict `ConfigSetting` row (`config_setting set speak '{"local":"all","slack":true}'`); the presence keys are omitted from the stored dict when empty. See §10.1.1. |

`notify_on_behalf` is NOT in this registry — it is derived (read-only),
set by `_apply_autonomy` under `autonomy = "notify"`, never a user toml key.

### 10.1.1 Local text-to-speech (#2060)

The `speak` config reads agent output aloud, gated on the macOS `say` binary (the
whole feature is inert when it is absent). DB-home (#1775, config-unify):
a JSON-dict `ConfigSetting` row, rebuilt bespoke by the resolver. Per-overlay
overridable via a `--overlay <name>` row that MERGES onto the global row; ad-hoc local
read via `t3 speak "…"`. The cold Stop hook reads the global row via `cold_reader`.

```bash
# local: "off" (default) | "dm" | "all" — what plays through this machine's speakers
# slack: false (default) | attach a spoken audio file to each Slack DM you receive
t3 <overlay> config_setting set speak '{"local": "all", "slack": true}'
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
double-play to suppress. The config lives in the DB store (read cold via
`cold_reader` on the Stop path); there is no other per-run state.

**Away-gate.** When availability resolves to `away` (§5.6.3), local playback is
silenced while the configured `slack` value is preserved — so no audio plays
through the local speakers while the user is unreachable, but a Slack-attached
rendition still reaches their phone. The gate lives at the PLAYBACK call site
(`speak._speak_local` consults `_is_away()`), not in `resolve_speak()`, so the
user's stored `speak` config is never mutated and every local consumer (`speak()`
and the local leg of `deliver_user_dm`) is gated by the one check. The away
check is exception-safe — a resolution failure is treated as **not** away (local
plays), so it can never spuriously mute audio or turn `slack` off.

**Meeting-mute (#2171).** Beside the away-gate, `_speak_local` also silences
local playback while a configured presence backend reports the user IN A
MEETING — same call-site gate, same Slack-arm exemption (a Slack-attached
rendition still reaches the phone). It is opt-in via `[teatree.speak]
presence_backend` (`""` = off, `msteams` = MS Teams) with the backend's access
token in the `pass` entry named by `presence_token_ref`. `teatree.core.presence`
resolves it: it probes the backend (`current_presence()`), caches the result
~60s, and returns `free` / `in_meeting` / `unknown`. Only a positive
`in_meeting` mutes; an unconfigured opt-in, an unreachable backend, or any probe
failure resolves to `unknown` and does NOT suppress (fail-safe to audible). The
MS Teams backend (`teatree.backends.msteams.presence`) reads MS Graph
`GET /me/presence` and maps `Busy`/`InAConferenceCall`/`Presenting` →
`in_meeting`; acquiring the delegated-`Presence.Read` Graph token is an operator
step. Core never imports the backend — `teatree.backends` registers the factory
at app-ready, the same inversion `backend_registry` uses.

**Question-mirror audio parity (#2171).** When `slack` is on, the AskUserQuestion
Slack mirror (§17.1 invariant 9) also carries a spoken rendition to the user's
phone, matching `notify_user` DMs. The router injects an audio enricher into the
`teatree.hooks.slack_mirror` leaf (keeping the leaf import-clean); after the text
question DM lands, the enricher spawns `t3 speak-dm` DETACHED (like the Stop-hook
`t3 speak` read) so synthesis never blocks the mirror's hook budget. Both
question surfaces — the present-mode mirror and the away-mode `DeferredQuestion`
capture — carry audio.

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

`mode` and `autonomy` are DB-home (#1775) — they live in the
`ConfigSetting` store. Set them there, globally or scoped to one overlay with `--overlay`:

```bash
t3 <overlay> config_setting set mode interactive                      # global default
t3 <overlay> config_setting set autonomy full --overlay t3-teatree    # single-author dogfooding: one switch collapses the gates + pins mode = auto
t3 <overlay> config_setting set autonomy notify --overlay t3-client   # collaborative: autonomous + DM per on-behalf action, keeps CLEAR merge gate
t3 <overlay> config_setting set mode interactive --overlay client-project   # stay gated on client code (autonomy defaults to babysit)
```

`privacy` is DB-home too — scope it to a client overlay:

```bash
t3 <overlay> config_setting set privacy '"strict"' --overlay client-project
```

### 10.1.2 Agent model tiering & session pins (`[agent]`)

The `[agent]` table holds the model/effort settings for spawned sub-agents and
the interactive main agent. It is read with a raw `tomllib` parse
(`config.agent_spawn.resolve_agent_config` + `model_tiering._load_phase_model_overrides`),
independent of the per-overlay `[teatree]` merge — these are session-scoped
spawn inputs, not overridable `UserSettings`.

```toml
[agent]
session_model = "opus"            # interactive main-agent --model pin (so you never run /model by hand)
session_effort = "xhigh"          # interactive main-agent --effort pin (strict CLI scale)

[agent.phase_models]              # per-PHASE model tier for the spawned sub-agent (model_tiering)
planning = "frontier"             # pin a phase up; "" / "default" / "inherit" opts out
reviewing = "sonnet"
testing = ""                      # explicit inherit (no --model)

[agent.skill_models]              # per-COMPANION-SKILL model floor (MODEL only, no effort axis)
code-review = "opus"              # a loaded skill RAISES the spawn model to at least this tier
architecture-design = "opus"
```

**`session_model` / `session_effort` (interactive main agent only).** Injected
as `--model` / `--effort` into the interactive `claude` spawn argv by
`t3 loop start` (`cli/loop.py`'s `os.execv`), so the main agent runs at the
pinned model/effort without a manual `/model`. This is the interactive
main-agent axis; the per-sub-agent reasoning effort is separate — a headless SDK
spawn gets its effort from its phase's abstract tier (`[agent.tier_effort]`, see
below), and `session_effort` never leaks into it (the in-session Agent tool has
no effort param). The effort scale is the strict CLI scale
`low | medium | high | xhigh | max` (`max` > `xhigh`; there is **no** `off`); an
off-scale value is a hard `ValueError` at parse and a `t3 doctor` FAIL.
"ultracode" (xhigh + auto dynamic workflows) is a session/settings concept, not
a value here. A model sentinel (`""` / `"default"` / `"inherit"`) means inherit
the default (no flag).

**`[agent.tier_effort]` (per-abstract-tier spawn effort).** The effort parallel
of `[agent.tier_models]`: overrides the reasoning effort an abstract tier spawns
with, merged OVER the shipped `model_tiering.TIER_EFFORT`
(`{"frontier": "xhigh", "balanced": "xhigh"}`; `cheap`/Haiku is absent → no
effort). `resolve_spawn_effort(phase)` resolves phase → tier → effort through the
same `[agent.phase_models]` override mechanism as the model, so a phase
downgraded to a cheaper tier drops model and effort together. Each value must be
a member of the effort scale (an off-scale or non-string value is dropped, the
same tolerance as `tier_models`). The headless SDK builder pins the result as
`ClaudeAgentOptions.effort=`; with this table absent the spawn effort is the
shipped per-tier default.

**`[agent.phase_models]` (per-phase sub-agent tier).** The shipped default pins
`planning → opus` and downgrades mechanical phases (`reviewing`/`requesting_review`/`testing`/`shipping`
→ `sonnet`, `retrospecting` → `haiku`); reasoning phases (`coding`, `debugging`)
inherit. Override any phase here; a sentinel opts it out.

**`[agent.skill_models]` (per-companion-skill MODEL floor).** Maps a companion
skill name to a model floor. When a dispatch loads that skill,
`model_tiering.resolve_spawn_model` raises the spawn model to the most capable
of the phase tier and every loaded skill's floor (most-capable-wins via
`cost.tier_rank`, capability order `haiku < sonnet < opus`; a floor only
RAISES, never downgrades). **MODEL only** — there is deliberately no per-skill
effort axis. With this table absent the spawn model is byte-for-byte the
per-phase tier. On an *inheriting* phase (`coding`/`debugging`, whose phase tier
is `None`) a floor raises the spawn model only when it is *strictly stronger*
than the assumed-opus inherited default — `tier_rank(None)` equals
`tier_rank("opus")`, so an `opus`-or-weaker floor is silently dropped (the phase
still inherits) and only a floor naming an unrecognised (assumed most-capable)
id pins it up. `t3 doctor` WARNs on a floor that names no known tier (likely a
typo) since an unknown id ranks most-capable.

Teatree deliberately carries no standalone "most expensive model" kill-switch
([#2237](https://github.com/souliane/teatree/issues/2237) removed the prior
single-toggle downgrade): the per-tier/per-phase routing above is already
explicit-opt-in — `TIER_MODELS` never NAMES a costlier-than-frontier model id by
default, so nothing routes to one without an operator writing the id into
`session_model` / `phase_models.<phase>` / `skill_models.<skill>` /
`honesty_model` themselves.

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
| `slack_scope_profile` | `Literal["full", "dm_only"]` | `"full"` | `"full"` = read/write-everywhere bot + shared xoxp user token. `"dm_only"` = a bot restricted to its one owner's DM: minimal bot scopes, no user token, no channel joins; the loader builds it with `owner_dm_only=True` so a non-owner destination fails loud |
| `slack_token_ref` | `str` | `""` | `pass` entry **prefix**; `<ref>-bot` and `<ref>-app` resolve the two tokens |
| `user_token_ref` | `str` | `""` | `pass` entry **full path** (NOT a prefix — read verbatim); holds the human's `xoxp-` user token for Slack-Connect reactions. A configured-but-unresolvable ref is flagged by `t3 doctor` (#3334) |
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

`default_logging(namespace)` in `config/loader.py` returns a Django `LOGGING` dict writing to `~/.local/share/teatree/<namespace>/logs/teatree.log` with rotation (5MB, 3 backups).

### 10.4 Data Storage

`~/.local/share/teatree/<namespace>/` — namespaced data directories created by `get_data_dir()`.

### 10.5 State Placement Rule — Cache vs Intent (#628)

**The DB `ConfigSetting` store is the source of truth for user config *intent*; other DB state is *derived* cache.** Config settings are authored directly in the store (§10.1.1). A NON-config datum may live DB-only **iff it can be deleted and deterministically rebuilt** from the config store plus repo state — deleting a derived row must lose no user intent. If losing a datum would lose user intent, it is config (authored in the store), not derived cache.

Consequences: genuinely bootstrap-readable settings (DB path, log level, `DJANGO_SETTINGS_MODULE`, the offline `private_repos` allowlist) resolve before the DB store is consulted — they must work with the store absent. User-authored config intent (mode, contribute, banned terms) lives in the `ConfigSetting` store. Derived/observational state (cached env values, last-seen branch, lifecycle phase history) is DB-as-cache and carries a regeneration path. The read/round-trip affordance is preserved by `t3 config show` — a read-only view of resolved config — and `t3 <overlay> config_setting export`, which dumps the store to a TOML backup.

**[#1775](https://github.com/souliane/teatree/issues/1775) — moving overridable config into the DB.** The `ConfigSetting` override tier (§10.1.1) deliberately lets the DB *own* user intent for an overridable setting, rather than only cache derived state. It stays inside this rule's spirit on two counts: (1) the DB is a strictly higher tier than the file — an empty table resolves byte-identically to today and the read fails safe to no-override when the DB is absent, so deleting the DB never loses the file-authored intent that remains the floor; and (2) the round-trip affordance is preserved by the `t3 <overlay> config_setting set|clear|list` admin path. The genuinely bootstrap-readable settings (`DATABASE_URL` / data-dir / `DJANGO_SETTINGS_MODULE` / `private_repos`) remain text-only — they must resolve before Django, so they can never move to this tier.
