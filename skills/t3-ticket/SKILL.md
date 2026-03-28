---
name: t3-ticket
description: Ticket intake and kickoff — from zero to ready-to-code. Use when user says "I have ticket X", "new ticket", "start working on", "what should I do for this?", or provides a ticket/issue/MR link.
compatibility: macOS/Linux, zsh or bash, git, glab or gh CLI for issue fetching.
requires:
  - t3-workspace
triggers:
  priority: 60
  keywords:
    - '(new ticket|start working|what should i do)'
    - '([a-z]+-\d+|\b(ticket|issue) #?\d+)'
  urls:
    - 'https?://gitlab\.[^\s]+/-/(issues|merge_requests|jobs)/\d+'
    - 'https?://github\.com/[^\s]+/(issues|pull)/\d+'
    - 'https?://(www\.)?notion\.(so|site)/'
    - 'https?://[^\s]*\.atlassian\.net/wiki/'
    - 'https?://linear\.app/[^\s]+/issue/'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Ticket Intake & Kickoff

## Delegation

> **Source:** Delegated skills originate from [obra/superpowers](https://github.com/obra/superpowers): writing-plans

This skill delegates the generic planning doctrine to:

- `writing-plans` — turn requirements into an execution plan before implementation starts

TeaTree keeps the project bootstrap locally: issue fetching, tenant detection, repo selection, and the worktree/lifecycle handoff to `t3-workspace`.

From zero to ready-to-code. Combines understanding the ticket with setting up the environment.

## Dependencies

- **t3-workspace** (required) — provides worktree creation, setup, and dev servers. **Load `/t3-workspace` now** if not already loaded.

## Workflow

### 1. Fetch Issue Context

- Fetch full issue description + ALL comments using the project's issue tracker (e.g., `glab issue view`).
- If linked to an MR: `glab mr view` for review-comment tasks.
- Download all embedded images from the issue. **Before reading any image**, validate it with `file <path>` — only read raster images (PNG, JPEG, GIF, WebP). Non-raster files (SVG, XML, HTML) or corrupt/empty files will poison the conversation context with unrecoverable "Could not process image" errors.
- For referenced external context (Notion, Slack, etc.): use CLI tools when available, MCP only for services without a CLI.
- **Deep-fetch linked pages:** When external context (Notion, Confluence, etc.) contains links to sub-pages, automatically fetch those too — including their discussions/comments. Don't wait for the user to ask. Enable `include_discussions: true` when fetching Notion pages to surface resolved discussions that clarify requirements.

### 2. State Acceptance Criteria

- Extract and list acceptance criteria before coding.
- If the ticket is vague, clarify with the user.

### 2b. Infer Deliverables (Non-Negotiable)

After extracting acceptance criteria, **proactively list all required artifacts** — don't wait for the user to tell you. Common deliverables to infer:

| Signal in the ticket | Likely deliverables |
|---|---|
| New business rule / validation | Code + data migration + translations + tests |
| New enum / resource type | Model + migration + serializer + admin + translation |
| Config/threshold change | Data migration (or flag for manual config) + tests |
| UI-visible change | Backend + frontend + translations |
| Tenant-specific behavior | Tenant override code + tenant-guarded migration |

Present the inferred list and let the user confirm or adjust before proceeding. This replaces asking "should I do X?" for obvious deliverables.

### 3. Select Playbook

- Match the ticket to a known playbook (from the project's playbook index).
- If no playbook matches, proceed with general workflow.

### 4. Select Scope

- Determine which repos are affected by the ticket.
- Load repository-specific references for each repo in scope.

### 5. Detect Variant/Tenant (Non-Negotiable for Multi-Tenant Projects)

**Always detect the target tenant before coding.** This determines environment setup, feature flag scope, and config repos.

1. **Check issue labels** — customer-name labels are authoritative, use directly.
2. **Check issue description** — explicit customer mentions or config-repo references.
3. **Check external tracker** — extract linked URLs from the issue description, fetch via MCP/CLI, look for customer/tenant properties. See project-specific skill references for the customer-name-to-variant mapping.
4. **Ask the user** — last resort, if none of the above yields a customer.

Pass the detected tenant to `t3 lifecycle setup <customer>` and `t3 lifecycle start <customer>`.

### 6. Create Worktree + Setup (Always — Don't Ask)

Worktree creation is the default for every ticket. **Never ask "should I create a worktree?"** — just do it after scope is confirmed.

Delegate to `/t3-workspace`:

- `t3 workspace ticket` — create worktrees for affected repos.
- `t3 lifecycle setup` — provision environment (symlinks, env, DB, direnv).

### 7. Start Dev Servers

Delegate to `/t3-workspace`:

- `t3 lifecycle start` or `t3 run backend`/`t3 run frontend` as needed.
- Verify services are running before declaring ready.

## Agent Rules

### User Hints Are Priority 1

When the user gives a debugging hint, **investigate that hint FIRST** before other theories. See `t3-debug/SKILL.md § Phase 0: User Hints` for the full protocol.

### Parallel Agent Dispatch

- **Use when:** 2+ independent problem domains, independent research, independent file modifications
- **Don't use when:** failures might be related, changes to shared state, need full system context first
- **Post-parallel:** review all summaries, check for conflicts, run full verification
