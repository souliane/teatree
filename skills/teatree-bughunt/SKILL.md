---
name: teatree-bughunt
description: Self-QA variant of batch mode — dogfood the teatree loop and statusline, find real bugs (missing signals, broken links, stale data, scanner errors), file them, then fix them in worktrees. Use when the user says "bug hunt", "self-qa", "hunt bugs", or "find and fix bugs".
metadata:
  version: 0.0.1
  subagent_safe: false
triggers:
  priority: 85
  keywords:
    - '\b(bug hunt|bug[- ]hunt|self[- ]qa|hunt (for )?bugs|find and fix bugs)\b'
    - '\bdogfood the loop\b'
search_hints:
  - bug hunt
  - self-qa
  - dogfood loop
  - hunt bugs
  - find and fix
---

# TeaTree — Bug Hunt Mode (Self-QA on the Loop and Statusline)

A Quick Wins variant where, instead of picking tickets off the board, the agent dogfoods the loop and the rendered statusline, finds bugs, files them, and fixes them in the same session. The user no longer has to play QA.

Shares the Quick Wins family with `/teatree-batch`.

## Prerequisites

Same as `/teatree-batch` (`ac-python`, `ac-django`, overlay skill loaded). Plus: at least one overlay registered with credentials that resolve (`t3 teatree loop tick --overlay <name>` must finish without `ImproperlyConfigured`); `~/.local/share/teatree/statusline.txt` writable (the renderer creates the directory if absent).

## Step 1 — Ask the scope

Use `AskUserQuestion` with three options:

- **Existing** — tackle open issues labelled `bug` from the board (no hunting).
- **New** — skip the board, dogfood the loop, file and fix whatever turns up.
- **Both** — existing first (they've already been triaged), then hunt for new ones.

Never silently pick one. The choice changes the workload materially.

## Step 2 — Run a tick (New / Both)

From the main clone — NOT a worktree. The goal is to QA the deployed state.

```bash
cd "$T3_REPO"

# One ad-hoc multi-overlay tick. JSON output is the structured surface; the
# rendered file is the user-visible surface. Inspect both.
t3 teatree loop tick --json > /tmp/tick.json
cat "${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt"

# Single-overlay diagnosis when a multi-overlay tick reports errors:
t3 teatree loop tick --overlay <name>
```

`tick.json` has `signal_count`, `action_count`, `errors`, and `actions[]` — the structured contract. The statusline file is the formatted contract — three zones (anchors / action_needed / in_flight), ANSI-coloured, OSC 8 hyperlinks where signals carry a `url` payload.

## Step 3 — Inspect both surfaces

Walk the output of the tick **and** the rendered statusline. The tick must agree with what the user actually sees at the bottom of their session.

**Structured (`tick.json` / `t3 teatree loop status`):**

- `errors` is empty. Any entry like `"my_prs[teatree]": "AuthError: ..."` is bug #1 — surface it before doing anything else.
- `signal_count` and `action_count` are non-zero when the registered overlays have open work. A clean inbox is plausible; a 0/0 tick on an overlay you know has open PRs is a scanner bug.
- Every scanner that should run on the active overlay shows up: `pending_tasks` (always, no overlay tag), `my_prs[<overlay>]`, `reviewer_prs[<overlay>]`, `assigned_issues[<overlay>]`, `slack_mentions[<overlay>]` if messaging is configured, `notion_view` if a Notion client is wired. A missing scanner with no error means the overlay declared it but the loop dropped it — file it.
- Every signal kind in the JSON is one the dispatcher knows about (see § "Reference"). An unknown kind silently falls through to `in_flight` — that's a bug, not a feature.

**Rendered (`statusline.txt`):**

- Anchor line first, dim grey, with `tick @ <iso>`. Missing → renderer broken.
- Action-needed lines bright red, in-flight bright cyan. If a `my_pr.failed` ends up cyan, the zone map drifted.
- Lines with a `url` payload render as OSC 8 hyperlinks — the raw bytes are `\033]8;;<url>\033\\<text>\033]8;;\033\\`. A line with a URL in JSON but **no** OSC 8 in the file is a render bug. (Run `od -c ~/.local/share/teatree/statusline.txt | head` if your terminal hides escapes.)
- Multi-overlay ticks prefix lines with `[<overlay>]`. A signal with `payload.overlay = "teatree"` and no `[teatree]` prefix is a `_zones_for` bug.
- `NO_COLOR=1 t3 teatree loop tick --statusline-file /tmp/x.txt` — the file must contain no `\033` bytes and URLs must fall back to `text <url>`.

### What counts as a bug (file it)

- **Scanner error in `report.errors`** — anything other than transient network failures. Tag the scanner+overlay in the title.
- **Missing signal** — open PR on the code host, ticket assigned to the user, fresh Slack mention, but the corresponding scanner emits nothing. Reproduce first, then file.
- **Wrong zone** — `my_pr.failed` rendered in `in_flight`, `slack.mention` rendered in `anchors`, etc. Cross-reference `_STATUSLINE_ZONE_BY_KIND` in `src/teatree/loop/dispatch.py`.
- **Broken hyperlink** — text URL where OSC 8 expected, OSC 8 wrapping the wrong text, hyperlink that points at the wrong PR.
- **Stale or duplicated entries** — same PR rendered twice, a closed PR still in `in_flight`, a merged PR in `action_needed`.
- **Multi-overlay leak** — a signal from one overlay rendered without (or with the wrong) `[overlay]` prefix; identical PRs from two overlays collapsed into one line.
- **Anchor / counter mismatch** — `signal_count` in JSON differs from the number of zone entries; `errors` non-empty but no "scanner errors:" line in `action_needed`.
- **Crash / non-zero exit** — `t3 teatree loop tick` raising a traceback to stderr is bug #1.

### What does NOT count (don't file)

- Empty zones on a quiet day.
- Subjective preferences about line phrasing or color choices (raise as enhancement, not bug).
- Transient network flakes (retry once; if it doesn't reproduce after 2 ticks, drop).
- Terminal-specific rendering quirks (some terminals don't honour OSC 8 — that's a terminal limitation, not a teatree bug, unless we're feeding it malformed escapes).

## Step 4 — Present findings before filing

List every bug with: source (`tick.json` field path, or statusline byte offset), symptom (what you saw vs. what you expected), probable cause if a quick `rg` makes it obvious, severity (blocker / high / medium / low). Ask the user to confirm the list — this waives the standing "never create tickets without asking" rule **only for the confirmed batch**.

Dedupe aggressively: if three findings share one root cause (one stale signal kind, one zone-map typo), file one ticket with all three symptoms listed.

## Step 5 — File and implement

For each confirmed bug, in severity order:

1. `gh issue create` with label `bug`, clear reproduction (paste the relevant `tick.json` excerpt and the rendered statusline line), severity, and the scanner / module to look at.
2. Add to the project board.
3. Implement per `/teatree-batch` rules (worktree via `t3 teatree workspace ticket`, TDD against the existing scanner/dispatch tests in `tests/teatree_loop/`, `t3:reviewer` sub-agent, sequential merge).
4. Close the issue via the PR.

## Step 6 — Tear down

Nothing to kill. Delete any temp files you wrote during inspection (`/tmp/tick.json`, `/tmp/x.txt`).

Report: bugs found, filed, fixed, skipped (with reasons).

## Reference — scanners, signal kinds, zones

Verify against the source before quoting in a bug report — these can drift.

- **Scanners** (`src/teatree/loop/scanners/`): `pending_tasks`, `my_prs`, `reviewer_prs`, `assigned_issues`, `slack_mentions`, `notion_view`. The first five run per-overlay when their backend is available; `pending_tasks` and `notion_view` run once.
- **Signal kinds** → **default zone / agent** (see `src/teatree/loop/dispatch.py`):
  - `my_pr.failed`, `my_pr.draft_notes` → `action_needed`
  - `my_pr.open` → `in_flight`
  - `slack.mention`, `slack.dm` → `action_needed` (also dispatched to `t3:reviewer` when the message body contains a PR URL)
  - `reviewer_pr.new_sha`, `reviewer_pr.unreviewed` → agent `t3:reviewer`
  - `pending_task`, `assigned_issue.ready` → agent `t3:orchestrator`
  - `notion.unrouted` → webhook `n8n`
  - Any unknown kind → `in_flight` (silent fallback — flag it)

## Rules

- The tick runs from the main clone, but all **fixes** happen in worktrees — don't edit the main clone.
- Bound the hunt: one pass through the JSON + rendered file. Don't loop the tick more than 2–3 times for the same scope — repeated ticks change `last_reviewed_sha` caches and other state.
- If `t3 teatree loop tick` won't run, that's bug #1 — file and fix it before continuing.
- Paste the relevant byte sequence in the issue body when the bug is render-shape (OSC 8, ANSI). `od -c` output is more useful than a screenshot here.
