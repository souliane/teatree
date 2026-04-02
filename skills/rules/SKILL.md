---
name: rules
description: Cross-cutting agent safety rules — clickable refs, temp files, sub-agent limits, UX preservation. Auto-loaded as a dependency by other skills.
compatibility: any
metadata:
  version: 0.0.1
  subagent_safe: true
---

# Agent Rules

Cross-cutting rules that apply to all teatree skills. Loaded automatically via `requires:`.

## User Instructions Are Priority 1 (Non-Negotiable)

When the user gives a direct, explicit instruction (`--no-verify`, skip tests, push now, use this approach), execute it IMMEDIATELY. Do not try a "better" approach first, do not retry the same failing approach hoping it works, and do not silently substitute your own plan. Execute the instruction first (it's fast and safe), then suggest an alternative if you have one.

## Context Transparency (Non-Negotiable)

The user cannot see system-reminders, memory content, or hook output injected into your context. When your response is influenced by any of this invisible context, **briefly state what you received** so the user can follow your reasoning. For example: "Teatree suggested loading `/t3:code`. Memory mentions X."

If the user's message is ambiguous (references "this", "it", a link they forgot to paste, etc.) — **ask for clarification**. Do NOT guess based on context the user can't see. Guessing leads to confusing exchanges where the user has no idea what you're talking about.

## Clickable References (Non-Negotiable)

Every MR, ticket, issue, or note reference — in markdown files, platform comments, **and** agent responses — must be a clickable markdown link.

- `[!5657](https://example.com/org/repo/-/merge_requests/5657)` — not `!5657`
- `[PROJ-1234](https://example.com/org/repo/-/issues/1234)` — not `PROJ-1234`

This applies everywhere: MR/PR descriptions, inline comments, test evidence, chat messages, and responses to the user.

## Token Extraction (Non-Negotiable)

When extracting an API token from a CLI tool, always extract to a variable first — never inline in curl. See your platform reference (`t3:platforms`) § "Token Extraction" for the platform-specific recipe.

**In Python heredocs:** shell variables are NOT inherited. Use `os.popen(...)` inside Python or `export TOKEN` before the heredoc.

## Temp File Safety (Non-Negotiable)

When using temporary files (for MR note bodies, test data, etc.):

- **Never use hardcoded paths** like `/tmp/mr_note_body.md` — stale content from other sessions gets posted to the wrong MR.
- **Always use `mktemp`** or inline Python heredocs instead.
- **Always use `>|`** (clobber override) not `>` — zsh `noclobber` silently prevents overwrite.
- **Always clean up** the temp file immediately after use (`os.unlink()` in Python, `rm` in shell).

## Complex API Payloads: Use curl or Python (Non-Negotiable)

Some issue tracker CLIs cannot serialize nested JSON. **Always use `curl`** with `-H "Content-Type: application/json"` and a proper JSON `-d` body for payloads containing nested objects.

For note bodies containing markdown images (`![alt](url)`), shell variable interpolation and `jq --arg` both escape `!` to `\!`. **Always use Python** (`urllib.request` or `requests`) to serialize the JSON payload.

## Preserve Existing UX Patterns (Non-Negotiable)

When fixing a broken UX mechanism (web terminal, browser launch, notification method), fix it **in-kind** — do not replace it with a different mechanism without asking. If proposing a different approach, ask the user first: "Currently this uses X. Want to keep that or switch to Y?"

## Never Post MR Comments from Parallel Agents (Non-Negotiable)

MR/PR comment posting (test plans, evidence, review notes) must be **serialized** — never dispatch two parallel agents that both post comments on MRs. Parallel agents cannot check for each other's posts, resulting in duplicate comments. Post all MR comments from the main conversation thread, or serialize agent tasks so only one posts at a time.

## Sub-Agent Limitations (Non-Negotiable)

Sub-agents (Agent tool) **lose all loaded skills, MCP access, and shell functions**. By default, never dispatch sub-agents for skill-dependent work. Do all skill-dependent work sequentially in the main conversation.

**Exception:** Skills with `subagent_safe: true` in their YAML frontmatter are pure methodology/guidelines that work without shell functions, MCP tools, or cross-skill state.

**Before delegating platform API work:** Read the relevant platform reference (`t3:platforms`) before writing sub-agent prompts that involve API calls (draft notes, discussions, MR operations). Sub-agents can't read skills themselves — copy the exact API recipe into the agent prompt.

**After a sub-agent completes, re-read any files it modified.** Sub-agents get a forked copy of your file state — their edits don't update your cache. Writing to a file without re-reading first will silently overwrite their changes.

## Prefer Native Tool APIs Over Filesystem Heuristics

When integrating with tools (issue trackers, CI, chat), prefer their API or CLI over scraping files. File-based approaches break on layout changes, don't handle pagination, and miss metadata.

## Symlink Safety

Never replace a symlink with a real file. `ls -la` first if unsure. If a path is a symlink, edit the target — never delete the link and write a new file.

## Shell Alias Safety

Use `command rm`, `command cp`, `command mv` in Bash tool calls to avoid zsh interactive aliases that hang. Also `gs` is aliased to `git status` — use `command gs` for GhostScript.

## Skill File Writes Require a Git Repo

Never modify skill files outside a git repo. Resolve real path with `readlink -f`, verify `git rev-parse --git-dir` succeeds. Changes to non-git copies are silently lost.

## Fix TeaTree/Skill Bugs Immediately (Non-Negotiable)

When a teatree or skill infrastructure bug is discovered during any task, fix it immediately as first priority. Never defer to focus on the user's task — broken infrastructure causes cascading failures.

## Ask About Auth Before External Service Integrations (Non-Negotiable)

When implementing features that require an external service (Notion, Slack, CI, etc.), ask "how do you authenticate with this service?" BEFORE writing any code. The answer (direct API token, CLI auth, MCP tool, OAuth, etc.) determines the entire architecture. Skipping this question leads to multiple implementation pivots.

## Never Change MR Base Branch or Dependencies (Non-Negotiable)

When an MR targets a non-default branch, that is intentional — it means the MR is part of a dependency chain. **Never** change an MR's target branch, rebase it onto a different base, or remove MR dependencies without explicit user instruction.

- If asked to "merge main" into a branch, merge the specified source — do not change what the branch is based on.
- If a branch is based on another feature branch (not main/master), keep it that way.
- If unsure about the dependency chain, **ask first**.

Destroying MR dependency chains wastes hours of carefully organized work.

## Always Create Tasks (Non-Negotiable)

On **every prompt**, use `TaskCreate` to create tasks before doing any work — even for a single task. Mark each task `in_progress` when starting, `completed` when done. Never skip this. Visible task tracking prevents forgotten steps and shows the user your progress.

## Always Use AskUserQuestion for Questions (Non-Negotiable)

**Never ask questions inline in text responses.** Always use the `AskUserQuestion` tool — it gives the user a structured UI to respond and prevents questions from being buried in output. One question at a time; wait for the answer before asking the next.

## Never Push Without Separate Explicit Approval (Non-Negotiable)

Commit approval ≠ push approval. Always ask "Push?" as a **separate question** after committing. This applies to all repos, all contexts — even when the user said "yes" to committing. (Safety net — source: `t3:ship § Never push without explicit approval`)

## Run Retro Before Ending Non-Trivial Sessions (Non-Negotiable)

Before ending any session that involved multi-file edits, debugging, or implementation work, run `/t3:next` (which includes `/t3:retro`). Do NOT wait for the user to ask — self-trigger this. A session without retro loses compound learning.

- **Trivial sessions** (single question, quick lookup, one-line fix): skip.
- **Everything else**: run `/t3:next` before your final response.

## Verify Imports Before Applying External Code (Non-Negotiable)

When cherry-picking code from orphan commits, stashes, snapshots, or other branches, verify every import and function call exists in the target codebase before applying. Snapshot code assumes a different state — modules, classes, and function signatures may not exist in HEAD. Apply each change surgically and run the type checker (`ty-check`) before moving on.

## Context Longevity

Long sessions lose context to automatic compaction. Proactively manage session length:

- **After 15+ tool calls**, suggest `/t3:next` or `/t3:retro` to preserve findings before compaction.
- **Before switching phases** (coding → testing, testing → reviewing), suggest wrapping up the current phase — phase transitions are natural breakpoints.
- **Re-reading a file you already read earlier** is a sign of context pressure. Consider wrapping up.
- **When context gets compacted**, critical state must survive — see the user's global agent config § Compact Instructions for what to preserve.

## Commit Before Declaring Done (Non-Negotiable)

When implementation is complete (all files written, tests pass or verified), **commit immediately** in the same response — do not wait for the user to ask. An uncommitted change is not "done"; it is in-progress work at risk of being lost to context compaction, parallel agents, or session timeout.

## Pre-Commit Hook Failures on Unrelated Tests (Non-Negotiable)

When a pre-commit hook runs the full test suite and fails on tests **unrelated to your changes** (pre-existing failures), do not fix them one by one in a loop. After the **second** unrelated failure, stop and tell the user: the hook is failing on pre-existing test issues, recommend `--no-verify` for this commit, and list the failing tests so they can be fixed separately.

## Worktree-First Work (Non-Negotiable)

**All development work MUST happen in a worktree**, never on the main clone. Use `t3 workspace ticket` or the `using-git-worktrees` skill to create one before writing any code.

**Collision detection — check on EVERY file write or git operation:**

1. Before writing to a file, run `git status`. If you see unexpected modifications to files you did not touch, **another agent is working in the same directory**.
2. **If you are NOT in a worktree:** STOP writing code. Move all your work to a worktree immediately (`t3 workspace ticket` or `EnterWorktree`), then continue there.
3. **If you ARE in a worktree and see someone else's changes:** STOP ALL WORK IMMEDIATELY. Alert the user: *"ALERT: Another agent is modifying files in my worktree at `<path>`. I've stopped all work to avoid conflicts. Please resolve before I continue."* Do NOT attempt to continue, merge, or work around the collision.

**Why:** Parallel agents modifying the same checkout cause silent data loss — commits overwrite each other, stashes destroy in-progress work, and merge conflicts go undetected. This has cost hours of wasted work. Worktrees give each agent an isolated copy. The rules below are secondary defenses.

## Concurrent Agent Safety (Non-Negotiable)

Assume another agent may be modifying the same repo concurrently. Never `git stash`, `git checkout --`, or `git restore` files you didn't change — this destroys the other agent's in-progress work. Only stage and commit files you explicitly modified.

## GitLab Inline Comments

When posting inline MR comments, target **added lines only** — not context or unchanged lines.
