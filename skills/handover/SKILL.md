---
name: handover
description: Use when the user wants to hand all current work from one Claude session to another (or to a not-yet-existing session) with a single command, or to transfer an in-flight TeaTree task from Claude to another runtime, or asks whether it is time to switch because Claude usage is getting high.
eval_exempt: describes a one-shot `t3 teatree handover` command surface; produces no recurring multi-step agent trajectory to grade
compatibility: any
requires:
  - rules
metadata:
  version: 0.0.2
  subagent_safe: false
---

# t3:handover

Two distinct hand-offs share this skill:

- **Session → session**: move all of THIS Claude session's in-flight work to another session (or to "whatever session starts next") with a single command, zero copy-paste. The receiving session picks it up automatically on start.
- **Claude → another runtime**: switch away from Claude (e.g. to Codex) without losing the thread, gated on five-hour usage telemetry.

## Session → session hand-off

The hand-off payload is the SAME durable-state snapshot the PreCompact hook builds (active tickets, worktree paths/branches, in-flight sub-agent ids+tasks, open PRs, approach/decisions, failing tests, loaded skills, t3-master status). A `SessionHandover` DB row is the source of truth; an XDG file (`[teatree] handover_mirror_path`, default `${XDG_STATE_HOME:-~/.local/state}/teatree/handover/latest.md`) mirrors it for human-readability and brand-new-session bootstrap.

### Hand off this session's work

```bash
t3 <overlay> handover create            # hand to the LIVE loop owner; if none, park for the next session
t3 <overlay> handover create --to <id>  # hand to a specific session id
```

No `--to` resolves the target to the live `t3-master` slot holder; if there is no live owner the hand-off is parked for whichever session starts next to claim. The row is always persisted AND mirrored to the XDG file.

### Know your own session id

A session needs its own id to be a `--to` target. Either:

```bash
t3 loop whoami            # prints THIS session's id
t3 loop owner             # prints "you are: <id>" plus who owns the loop
t3 <overlay> handover whoami
```

### Takeover (automatic, zero copy-paste)

A fresh / non-owner session claims an unclaimed hand-off (targeted at it, or parked for "next session") on `SessionStart` and injects the payload as `additionalContext` — no command needed. The claim is marked once so it injects exactly once. `t3 <overlay> handover claim-on-start --session <id>` is the hook entry point; you do not normally run it by hand.

## Session recovery — MCP connectors after a network change, account switch, or restart

Handovers cluster around the moments that break MCP: a `/login` account switch, a session
restart, or a transient network change (e.g. a VPN toggled off for a moment). The receiving
session — or the same session after the switch — needs this recovery procedure, because dead
MCP tools silently block any interactive work that depends on them (an optional connector like
Notion gates connector-driven work, and the failure is silent).

This recovery is only for the **optional** claude.ai connectors an interactive session (or an
overlay) leans on — it is not a teatree runtime dependency. Teatree's own runtime Slack posts
through the **direct Slack API** with a `pass`-stored token (never the claude.ai Slack connector),
so a wedged connector never blocks teatree's runtime; the browser tool is now chrome-devtools-mcp,
which drives its own Chrome and needs no connector recovery at all. So a down connector only
affects connector-driven interactive work.

**Symptom.** A claude.ai connector (e.g. Notion, or an optional Slack/Sentry/Drive connector an
overlay uses) shows connected in `claude mcp list` / `t3 doctor`, but the in-session MCP tools are
dead — calls fail, and a `/mcp` reconnect returns `HTTP 404 at https://mcp.notion.com/mcp` or
"Authentication successful, but server reconnection failed." The OAuth tokens are stored fine;
it is the in-process socket/handshake that went stale. A short VPN drop or an account switch is
enough to wedge it.

**Fix (in-session, NO restart needed).**

1. Re-run **`/login`** — this re-registers the claude.ai built-ins and re-drives the OAuth
   handshake that `/mcp` alone cannot. `/mcp` re-auth by itself does **not** recover a wedged
   socket; `/login` does.
2. If the first `/login` does not flip the connectors to usable, **run `/login` a second time** —
   a second pass has recovered it when the first did not.
3. Confirm with a read-only MCP probe (e.g. a Notion `get-teams` or a Slack channel search), not
   just `claude mcp list` — the list can show ✔ while the socket is still dead.

Do **not** restart to fix this — a restart kills in-flight background sub-agents (E2E runs,
coders) for nothing. Durable state survives a restart anyway (open PRs live on the forge, harness
tasks and the PreCompact snapshot persist), so if a restart is ever needed, let in-flight runs
finish first. Upstream context: the Notion-side OAuth regression that caused the 404 was fixed in
Claude Code ≥ 2.1.136; on a current build, `/login` is the reliable in-session recovery.

## Claude → another runtime

Use this when the user wants to switch away from Claude without losing the thread.

### 1. Check Claude usage

Run:

```bash
t3 tool claude-handover --json --current-runtime <runtime>
```

Read these fields from the JSON:

- `current_runtime`
- `preferred_runtime`
- `recommended_runtime`
- `five_hour_used_percentage`
- `should_handover`
- `five_hour_resets_at`

Tell the user the current five-hour usage, which runtime currently has priority, and whether TeaTree recommends switching now.

### 2. Ask before switching

If the user did not explicitly request an immediate switch, ask one short question:

- continue on Claude
- switch now

Do not switch runtimes silently.

### 3. Prepare the handover bundle

Before ending the Claude session, produce a compact handover brief with:

- current goal
- exact repo and branch
- files already changed
- tests already run and their results
- open blockers or unanswered questions
- the next concrete action for the new runtime

Prefer a plain markdown summary in the conversation. If the user asks for a file, write a short markdown handoff note in the repo root or `artifacts/`.

### 4. Make the next runtime efficient

When handing off to Codex or another runtime:

- include the handover brief verbatim
- include the exact command or test that should be run first
- say explicitly that Claude session IDs are not portable across runtimes
- point to the latest TeaTree telemetry if relevant

## Rules

- Do not claim another runtime can resume a Claude session directly.
- Do not hide the recommendation threshold from the user.
- If telemetry is missing, say so and fall back to a manual summary-based handover.
