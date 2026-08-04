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

The payload has three sources, in order: what the session **authored** (`--from-file` / `--body`), else the durable-state snapshot the PreCompact hook builds (active tickets, worktree paths/branches, in-flight sub-agent ids+tasks, open PRs, approach/decisions, failing tests, loaded skills, t3-master status), else a payload **derived** from live DB state (worktrees, active tickets, open PRs) so a session that has neither authored nor compacted still hands over its in-flight work. A `SessionHandover` DB row is the delivery surface; a file mirror (the DB-home `handover_mirror_path` setting, default `${T3_DATA_DIR:-${XDG_DATA_HOME:-~/.local/share}/teatree}/handover/latest.md`) mirrors it for human-readability and is READ only when the DB is unreachable — never because the drain came back empty, which is how four pending rows were passed over while one stale file was injected in their place. The mirror lives under the SHARED data dir on purpose — it is the one directory the host and the worker container both see, so a hand-off created on either side bootstraps a session on the other. The `latest.md` pointer resolves to the newest mirror IN that directory (filenames embed a fixed-width `created_at` stamp, so lexicographic order is chronological), not to whichever file happened to be written last.

### Hand off this session's work

```bash
t3 <overlay> handover create --from-file handover.md            # hand over what YOU wrote (use '-' for stdin)
t3 <overlay> handover create --body "$(cat handover.md)"        # same, inline
t3 <overlay> handover create --from-file handover.md --to <id>  # …to a specific session id
t3 <overlay> handover create                                    # no body: derived, and reported UNVETTED
```

**Write the body.** A hand-off exists to carry this session's REASONING — the standing constraints, the current approach, what must not be resumed by hand. Nothing else can re-derive it; if the DB could, the hand-off would not be needed. Pass exactly one of `--from-file` / `--body`; an unreadable `--from-file` is refused rather than silently falling back to a derived payload.

No `--to` resolves the target to the live `t3-master` slot holder; if there is no live owner the hand-off is parked for whichever session starts next to claim. A payload that resolves to something is persisted AND mirrored to the file; one that resolves EMPTY writes neither.

**One row per session, and a second `create` ADDS to it.** A session holds at most one unclaimed hand-off — a partial unique index enforces it, so no call site can reintroduce the fan-out that once left a receiver three partially-contradictory narratives from one author. A later `create` lands on that same row and appends behind a fence rather than overwriting: replacing the ROW must not replace the STATE. The JSON reports `updated_existing` and `previous_payload_bytes` so the absorb is never silent.

**`create` runs the sub-agent barrier and folds its returns INTO the payload.** Every in-flight sub-agent worktree is driven through leak-gated fast-push first, and each agent's done/remaining lands in the persisted row — the receiver reads the row, so returns that are only printed reach nobody. With no agents in flight the section still renders an explicit line, because an absent section reads exactly like a barrier that never ran. `--no-drive-subagents` skips the barrier and writes no section. The barrier also runs on the refused paths, including the `empty` one: rescuing a sub-agent's unpushed work is orthogonal to whether this session has anything to hand over, and it is the session with nothing to say that is most likely to be stranding some.

**ONE wrap-up section per row, updated in place, carrying every agent ever seen.** A second `create` UPDATES that one section instead of appending another, so ten hand-offs leave one line-up rather than ten snapshots to reconcile. It is rendered from a stored union keyed on worktree path, so an agent enumerated at an earlier barrier and absent from the latest one is still named, with its last-known status and an explicit not-enumerated-at-the-latest-barrier marker — absence is ambiguous, and "its worktree is gone with unpushed work on it" is the case that matters. The barrier's own worktree, and the whole checkout the command runs in, are excluded from the fast-push.

**`create` verifies the row before it reports OK.** After writing, the row is re-read from the DB and asserted complete — non-empty, carrying the resolved bytes, unclaimed, addressed to somebody who can claim it, carrying exactly one sub-agent wrap-up section, and the only unclaimed row for this author. A failure emits `"completeness_ok": false` with the reasons and exits 1, so no OK line is ever printed over a row that does not hold the state. The JSON carries `handover_id`: the INTEGER row primary key.

**`handover create` reports what it actually transferred.** The JSON carries `payload_source` — `authored`, `snapshot`, `live-state`, or `empty` — and only the first two report `"ok": true`:

| `payload_source` | exit | meaning |
|---|---|---|
| `authored` / `snapshot` | 0 | a vetted payload: the session wrote it, or its own PreCompact snapshot holds it |
| `live-state` | 3 | recorded, but DERIVED — carries the in-flight inventory and none of the reasoning. Re-run with `--from-file`. |
| `empty` | 1 | nothing to transfer at all; **no row is written**, so nothing can be delivered — `handover_id` is `null` and `row_written` is `false` |

An unvetted or empty payload never reports OK: the operator moving on believing state was carried over is the failure this command exists to prevent. The `empty` refusal writes NOTHING — no row, no mirror, and no mutation of this author's existing unclaimed row. A zero-agent barrier result is a negative fact about a hand-off, not a hand-off; a row carrying only it would arrive under the SESSION HAND-OFF RECEIVED directive transferring nothing, and would consume the author's single unclaimed slot.

**A hand-off addressed to its own session is refused** (exit 1). `claimable_for` admits only the session named by `to_session` and excludes the session named by `from_session`, so a self-addressed row is claimable by nobody — it would sit as "pending" forever. Omit `--to` to park it for the next session instead.

### Know your own session id

A session needs its own id to be a `--to` target. Either:

```bash
t3 loop whoami            # prints THIS session's id
t3 loop owner             # prints "you are: <id>" plus who owns the loop
t3 <overlay> handover whoami
```

### Takeover (automatic, zero copy-paste)

A fresh / non-owner session DRAINS every hand-off claimable by it (targeted at it, plus everything parked for "next session") from the DB on `SessionStart` and injects the payload as `additionalContext` — no command needed. The file mirror is the bootstrap transport for a process that cannot reach the DB, never the carrier: a readable DB that yields nothing delivers nothing and leaves the mirror untouched. There are three states, and delivery happens on two of them — reachable-and-mine (the drain's answer is the answer), reachable-but-DIVERGED (the DB opened but is not the one hand-offs go to, because `resolve_data_dir` auto-isolates per worktree while the mirror stays shared: nothing is delivered and a WARNING names both DBs, and the mirror is deliberately NOT read, since an auto-isolated DB can legitimately BE the delivery DB and reading the shared mirror would trade a silent miss for a wrong delivery), and unreachable (the mirror bootstraps the session, loudly). Each claim is marked once so it injects exactly once. `t3 <overlay> handover claim-on-start --session <id>` is the hook entry point; you do not normally run it by hand.

The queue is drained, not sampled: a hand-off targeted AT this session leads (more specific than the open broadcast), then the parked tier follows OLDEST-first, so the backlog makes progress instead of one newest row starving every older one forever. When several hand-offs arrive together each renders behind its own `## Hand-off N of M — from <session>` fence, so the receiver does not read N authors' state as one narrative. Both pickup call sites go through the single `handover.claim_handovers` seam, so neither can drift back to a claim-one policy.

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
