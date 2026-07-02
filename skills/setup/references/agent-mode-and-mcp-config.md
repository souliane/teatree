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

## 2. Overlay-declared MCP tool requirements

Each overlay integration (issue tracker, Slack, Notion, observability
tooling) reaches the agent through MCP tools whose call patterns must match
a `permissions.allow` entry in the user's `~/.claude/settings.json` to run
without a per-call prompt.

What teatree models today, verified against code:

- An overlay declares its messaging integration through `OverlayConfig`
  (`teatree.core.overlay.OverlayConfig`): `messaging_backend`
  (`"noop"` default, `"slack"` opt-in), `slack_token_ref`,
  `slack_user_id`. These are set as `UPPER_CASE` constants in an
  `overlay_settings` module or as `lower_case` keys under
  `[overlays.<name>]` in `~/.teatree.toml` (see
  `OverlayConfig.apply_toml_overrides`). `t3 setup slack-bot --overlay
  <name>` provisions the Slack app and stores the two tokens in `pass`
  under `<slack_token_ref>-bot` / `<slack_token_ref>-app`.
- The plugin's own `settings.json` ships a **broad Bash `permissions.allow`
  / narrow `deny`** list (BLUEPRINT.md § 11.4) so every command teatree
  and its overlays legitimately invoke matches a static rule. It ships
  **no** `mcp__*` allow entries and **no** classifier `autoMode` /
  `defaultMode` block — by design (§ 11.4: plugin config is not
  self-modifiable by the agent; classifier rules stay per-user).

The expected pattern for the MCP-tool permission entries an overlay's
integrations need (e.g. `mcp__<server>__*`) is therefore: the user adds
them to **their own** `~/.claude/settings.json` `permissions.allow`. The
overlay documents which MCP servers it depends on in its own `SKILL.md` /
README; the user provisions the matching allow entries once. Teatree does
not auto-write the user's settings file for MCP entries any more than it
does for Bash entries — the same self-modification guardrail applies (§
11.4).

> There is currently no `OverlayConfig` attribute through which an overlay
> declares an explicit list of required `mcp__*` permission patterns for
> `t3 setup` to provision automatically. Adding one is tracked under the
> umbrella issue #836 / #854; until it lands, the documented pattern above
> (overlay README + user-owned `permissions.allow`) is the expected setup.
> This note is deliberately honest about the boundary rather than
> describing a mechanism that does not exist.

## 2.1 Enabled-MCP connectivity check (souliane/teatree#2282)

An MCP server the user has *enabled* but whose live connection is broken is a
silent failure: tool calls against it fail late, mid-task, with no obvious
root cause. Teatree verifies connectivity at three surfaces, all routed
through the single chokepoint `teatree.core.mcp_connectivity.check_mcp_connectivity`:

| Surface | Where |
|---------|-------|
| Session start | The `SessionStart` hook surfaces a run-the-check advisory whenever any MCP server is enabled (a cheap, network-free `~/.claude.json` read — even within the 30s hook budget the live `claude mcp list` probe is kept off the every-session start path, where a slow or hung MCP endpoint would stall every session; the probe is deferred to `t3 doctor check` below). |
| `t3 doctor check` | The `_check_mcp_connectivity` gate live-probes (`claude mcp list`) and FAILs on any enabled-but-disconnected server or provider mismatch. |
| Account switch | `t3 setup recover-account-switch` re-runs the same check after a `/login`, so a switch that left an enabled MCP disconnected exits non-zero. |

The check enumerates every *enabled* server (top-level + project-scoped
`mcpServers` plus the claude.ai-hosted connectors in
`claudeAiMcpEverConnected`, minus the per-project `disabledMcpServers` set),
live-probes each one's connected status, and validates each resolves to its
overlay-*declared* provider. A disconnected enabled server, or a provider
mismatch, is a LOUD, named finding with a reconnect hint — never a silent pass.
A probe that cannot run (`claude` absent) degrades to a WARN.

An overlay declares the **expected provider** per MCP server via
`OverlayBase.get_mcp_provider_expectations() -> dict[str, str]` (default `{}`),
mapping a server name to either `CLAUDE_AI_HOSTED` (a `claude.ai <Service>`
connector served from an Anthropic-hosted endpoint) or `THIRD_PARTY` (a
self-hosted or local-command server). Teatree's own default is empty — the
connectivity check enforces only connected-ness until an overlay supplies
per-server values. The real per-overlay provider values live in the overlay
repo (souliane/teatree#251); core ships only the extension point and the
validation logic.

## 2.2 Teatree's own bundled structured-search MCP server (souliane/teatree#2863)

Teatree ships its own MCP server (`t3 mcp serve`, [#1023](https://github.com/souliane/teatree/issues/1023)) as a **plugin-bundled** server: a `.mcp.json` at the repo root (sibling of `.claude-plugin/`), the same convention official Claude Code plugins use for a bundled server. Claude Code starts a plugin-bundled server automatically once the plugin is enabled — `t3 setup` already enables the plugin (§ 3 below), so no separate `claude mcp add` step registers this one. Its five read-only tools (`ticket_search`, `worktree_status`, `pr_for_ticket`, `loop_stats`, `incoming_event_recent`) surface as `mcp__teatree__*`.

This is **not** covered by the § 2.1 enabled-MCP connectivity check: `read_enabled_mcp_servers` reads only `~/.claude.json`'s `mcpServers` + `claudeAiMcpEverConnected`, and a plugin-bundled server is never written there. `teatree.core.mcp_registration.verify_teatree_mcp_registration` is the dedicated, structural check both `t3 setup` (an `OK`/`WARN` confirmation line) and `t3 doctor check` (`_check_teatree_mcp_registration`, which additionally live-probes via `claude mcp list` when `claude` is on PATH) read — the single chokepoint so the two surfaces cannot drift on what "correctly registered" means.

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
`report_missing_authorizations`), called from `teatree.cli.setup.run`
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
| Enabled-MCP connectivity check | n/a — runs at session start / `t3 doctor` / account-switch | `teatree.core.mcp_connectivity.check_mcp_connectivity` (#2282) |
| Per-server expected provider | overlay's `get_mcp_provider_expectations()` (real values in #251) | `teatree.core.overlay.OverlayBase` |
| Teatree's own bundled MCP server | n/a — ships in `.mcp.json`, auto-starts with the plugin | `teatree.core.mcp_registration` (#2863) |
| Recommended auto-mode set | suggested only — user pastes into `autoMode.allow` | `teatree.cli.recommended_authorizations` |
