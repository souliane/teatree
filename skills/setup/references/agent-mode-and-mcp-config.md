# Agent mode, MCP tooling, and permissions — the config surface

Several categories of agent behaviour are legitimately instance-specific:
whether the session runs autonomously, which MCP integrations an overlay
needs, and which standing permissions make a session friction-free. Before
this reference these were scattered across the codebase, BLUEPRINT.md, and
each user's personal config, so each user reinvented the same setup. This
page collects the **actual** config surface in one place and points at the
module that owns each knob, so nothing here drifts from the code.

Companion reference: the recommended-but-never-shipped auto-mode set is
documented separately in
[`recommended-automode-authorizations.md`](recommended-automode-authorizations.md).
This page is the map; that page is the paste-ready list.

## 1. Operating mode (`mode`)

The auto-vs-interactive choice is a single teatree config knob, **not** an
agent-runtime setting the user hand-edits. `mode` is a DB-home setting — set
it in the `ConfigSetting` store (or via the `T3_MODE` env var), not in TOML:

```bash
t3 <overlay> config_setting set mode interactive   # or "auto"; add --overlay <name> for a per-overlay value
```

| Value | Behaviour |
|-------|-----------|
| `interactive` | Default. Conservative on security — every publishing action (push, PR create/merge, external write) stops and asks. |
| `auto` | Full autonomy end-to-end; falls back to interactive only for the always-gated non-negotiables (force-push to default branches, destructive shared-state ops). |

Resolution (first match wins): the `T3_MODE` env var, then the active
overlay's per-overlay `mode` value, then the global `mode` value, then the
dataclass default `interactive`. Both per-overlay and global values come from
the `ConfigSetting` DB store (`config_setting set mode … [--overlay <name>]`);
the `[teatree] mode` / `[overlays.<name>] mode` TOML keys are ignored on read.
Source of truth:
`teatree.config` — the `Mode` enum, `UserSettings.mode`,
`OVERLAY_OVERRIDABLE_SETTINGS`, `ENV_SETTING_OVERRIDES`, and
`get_effective_settings()`. Invalid values raise rather than silently
downgrading to a less-safe mode (`Mode.parse`).

There is intentionally no `[agent] defaultMode` key: per-installation
auto-vs-interactive is the global `mode` value, and per-overlay (e.g. a
headless overlay running auto while another stays interactive) is the
per-overlay `mode` value — both set via `config_setting set mode`
(`--overlay <name>` for the per-overlay scope). The loop honours the active
overlay's resolved mode (BLUEPRINT.md § 5.6.2).

### Training wheels for `auto`

`auto` mode has two opt-out gates so a freshly-autonomous installation does
not publish irreversibly without a human in the loop. Both are DB-home
booleans, per-overlay overridable, defaulting to `true` (source:
`teatree.config.UserSettings`) — set each with `t3 <overlay> config_setting
set <key> false` (add `--overlay <name>` for a per-overlay value):

| Key | Effect when `true` |
|-----|--------------------|
| `require_human_approval_to_merge` | Loop pushes and opens PRs autonomously but merge waits for a 👍 / `/merge`. |
| `require_human_approval_to_answer` | The `t3:answerer` capability drafts a reply, DMs the user, posts only on confirmation. |

The user flips either to `false` only once comfortable. No effect in
`interactive` mode (everything prompts there regardless).

## 2. The one MCP server: teatree's own

teatree depends on exactly one MCP server, its own (§ 2.2). Every third-party
integration an overlay needs — Slack, Notion, GitHub/GitLab, Sentry, SharePoint —
is a teatree MCP service tool group backed by teatree's own credentials, and a
`t3` CLI command; browser work is `t3 browser` (Playwright). Nothing depends on a
claude.ai-hosted or third-party MCP server, so nothing needs reconnecting after a
`/login` or a network change.

What teatree models, verified against code:

- An overlay declares the services it needs as data, in
  `OverlayConfig.required_third_party_services` (a set of
  `teatree.backends.types.Service`). The MCP server registers a service's tool
  group only when some registered overlay declares it, and resolves its client
  through `teatree.mcp.service_resolver.SERVICE_CLIENTS` — the first declaring
  overlay with configured credentials.
- An overlay declares its messaging integration through `OverlayConfig`
  (`teatree.core.overlay.OverlayConfig`): `messaging_backend`
  (`"noop"` default, `"slack"` opt-in), `slack_token_ref`,
  `slack_user_id`. These are set as `UPPER_CASE` constants in an
  `overlay_settings` module or as `lower_case` keys in the overlay's
  DB `overlays` registry row. `t3 setup slack-bot --overlay
  <name>` provisions the Slack app and stores the two tokens in `pass`
  under `<slack_token_ref>-bot` / `<slack_token_ref>-app`.
- The plugin's own `settings.json` ships a **broad Bash `permissions.allow`
  / narrow `deny`** list (BLUEPRINT.md § 11.4) so every command teatree
  and its overlays legitimately invoke matches a static rule. It ships
  **no** `mcp__*` allow entries and **no** classifier `autoMode` /
  `defaultMode` block — by design (§ 11.4: plugin config is not
  self-modifiable by the agent; classifier rules stay per-user). The user
  allows teatree's own tools (`mcp__plugin_t3_teatree__*`) once in their own
  `~/.claude/settings.json`.

## 2.1 What `t3 doctor` checks

| Check | Verdict |
|-------|---------|
| `_check_teatree_mcp_registration` | WARN when the plugin-bundled `.mcp.json` no longer declares `teatree` as `t3 mcp serve` (§ 2.2). |
| `_check_declared_services_configured` | FAIL for each service an overlay declares that teatree holds no credentials for, naming the service and the declaring overlays — resolved through the same `SERVICE_CLIENTS` builders the tools use. |
| `_check_teatree_mcp_liveness` | FAIL when the registered `t3 mcp serve` does not answer a real `initialize`, naming the cause and the remedy. |

A Slack write tool on any MCP server but teatree's is refused at `PreToolUse`
(`hooks/scripts/mcp_slack_write_guard.py`) and redirected to the `t3` CLI;
teatree's own server is exempt by its exact server name, because its Slack tools
run through the same backends and egress gates.

## 2.1.2 Token-scope-failure cache (PR-19)

A backend call that fails on a missing OAuth **scope** is a hard config failure,
not transient — retrying it this loop process fails identically. The in-process
`teatree.core.intake.scope_cache` records each known-missing `(token_id, scope)` pair so
a guarded call short-circuits **pre-HTTP** on the second and later attempts
(`ScopeMissingError(cached=True)`, zero network), and the first failure emits
exactly ONE bot→user banner (idempotency key `scope_missing:<token_id>:<scope>`).
`token_id` is a sha256 fingerprint — the literal token never enters the cache, a
log line, or a banner. The Slack transport (`SlackHttpClient`) consults it via a
static method→scope map (`reactions:write`, `chat:write`); the cache resets each
tick (a new tick re-tests every scope) and `t3 doctor authorizations` clears it on
a re-check.

## 2.2 Teatree's own bundled structured-search MCP server (souliane/teatree#2863)

Teatree ships its own MCP server (`t3 mcp serve`, [#1023](https://github.com/souliane/teatree/issues/1023)) as a **plugin-bundled** server: a `.mcp.json` at the repo root (sibling of `.claude-plugin/`), the same convention official Claude Code plugins use for a bundled server. Claude Code starts a plugin-bundled server automatically once the plugin is enabled — `t3 setup` already enables the plugin (§ 3 below), so no separate `claude mcp add` step registers this one. Its tools surface as `mcp__teatree__*` and are **read + gate-preserving writes**, not read-only: ~13 read tools (`ticket_search`, `ticket_get`, `ticket_list`, `worktree_status`, `pr_for_ticket`, `task_list`, `loop_stats`, `command_search`, `config_setting_get`, `gate_status`, `factory_signals`, …) plus the write suite (`pr_create`, `pr_merge`, `notify_user`, `config_setting_set`, the review-post and per-service forge/slack writes). Each write wraps the exact seam the `t3` CLI calls, so the same FSM / merge / on-behalf / leak gates fire — the server does not expose an ungated write path. Per-service groups register only when an overlay declares the service.

`teatree.core.mcp_registration.verify_teatree_mcp_registration` is the structural check both `t3 setup` (an `OK`/`WARN` confirmation line) and `t3 doctor check` (`_check_teatree_mcp_registration`) read — the single chokepoint so the two surfaces cannot drift on what "correctly registered" means. Whether the server actually runs is the doctor's liveness check (§ 2.1).

## 3. Standing permission state after `t3 setup`

`t3 setup` does **not** set `skipDangerousModePermissionPrompt` or
`skipAutoPermissionPrompt`, and teatree ships no `auto-approve-*.sh`
hooks. Day-to-day friction is removed instead by the broad
`permissions.allow` list in the plugin's `settings.json` (so the
classifier is never consulted for routine workflow) plus a **read-only**
suggestion pass: at the end of every `t3 setup`, and via `t3 doctor
authorizations`, teatree detects which generic recommended auto-mode
authorizations are absent from the user's resolved `~/.claude/settings.json`
`autoMode.allow` and prints the paste-ready sentence for each missing one.

Source of truth: `teatree.cli.recommended_authorizations`
(`RECOMMENDED_AUTHORIZATIONS`, `find_missing_authorizations`,
`report_missing_authorizations`), called from `teatree.cli.setup.command.run`
and registered as a `t3 doctor` command in `teatree.cli.doctor`.
Detection never writes the user's settings file. The full set and the
rationale are in
[`recommended-automode-authorizations.md`](recommended-automode-authorizations.md).

## 4. Dev-environment lifecycle authorizations

Auto-approving `docker`, `pkill`, `docker compose`, lifecycle, and
verification commands is **not** something each overlay user builds from
scratch. It is one of the recommended generic authorizations:
`local-dev-lifecycle-commands` in
`teatree.cli.recommended_authorizations.RECOMMENDED_AUTHORIZATIONS`, which
covers `pkill, docker, docker compose, pipenv, playwright, npm, npx,
curl, sed` run as part of a t3 lifecycle or verification step. The
expected setup is to paste that suggested sentence into the user's own
`autoMode.allow`; `t3 doctor authorizations` reports it as missing until
present.

## 5. Self-modification of the t3 ecosystem

Standing authorization for the agent to edit teatree and overlay skill
files (and `~/.claude/settings.json` / `~/.claude/hooks/`, which teatree
manages) is the recommended `manage-claude-settings-and-hooks` and
`worktree-file-writes` authorizations (same module, same `t3 doctor
authorizations` flow). The agent-facing behaviour when the classifier
denies a call mid-session is the **Classifier Denial Protocol** in
`skills/rules/SKILL.md` — that section is canonical for *reacting* to a
denial; this page is about the *standing* config surface. Neither
duplicates the other.

## Quick map (key → owning module)

| Config surface | Where the user sets it | Code owner |
|----------------|------------------------|------------|
| Operating mode | `config_setting set mode …` (global) / `--overlay <name>` / `T3_MODE` | `teatree.config` (`Mode`, `get_effective_settings`) |
| Auto-mode training wheels | `config_setting set require_human_approval_to_* …` (global / `--overlay <name>`) | `teatree.config.UserSettings` |
| Overlay messaging integration | `[overlays.<name>]` keys / `overlay_settings` module | `teatree.core.overlay.OverlayConfig` |
| Bash standing permissions | plugin `settings.json` (broad allow / narrow deny) | `settings.json` (BLUEPRINT.md § 11.4) |
| MCP / auto-mode permissions | user's own `~/.claude/settings.json` | not plugin-shipped, by design (§ 11.4) |
| Third-party services an overlay needs | `OverlayConfig.required_third_party_services` | `teatree.mcp.service_resolver.SERVICE_CLIENTS` + `t3 doctor`'s declared-services check |
| Account-switch recovery | `t3 setup recover-account-switch` | `teatree.core.account_switch` (#1916) |
| Token-scope-failure cache | n/a — in-process, per loop tick; cleared by `t3 doctor authorizations` | `teatree.core.intake.scope_cache` (PR-19) |
| Teatree's own bundled MCP server | n/a — ships in `.mcp.json`, auto-starts with the plugin | `teatree.core.mcp_registration` (#2863) |
| Recommended auto-mode set | suggested only — user pastes into `autoMode.allow` | `teatree.cli.recommended_authorizations` |
