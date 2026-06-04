---
name: ticket
description: Ticket intake and kickoff — from zero to ready-to-code. Use when user says "I have ticket X", "new ticket", "start working on", "what should I do for this?", or provides a ticket/issue/PR link.
compatibility: macOS/Linux, zsh or bash, git, glab or gh CLI for issue fetching.
requires:
  - workspace
  - architecture-design
  - teatree
companions:
  - writing-plans
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
search_hints:
  - ticket
  - issue
  - start working on
  - intake
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Ticket Intake & Kickoff

## Delegation

This skill delegates the generic planning doctrine to:

- `writing-plans` — turn requirements into an execution plan before implementation starts

Optional [obra/superpowers](https://github.com/obra/superpowers) companion provides generic methodology. TeaTree keeps the project-specific workflow locally.

From zero to ready-to-code. Combines understanding the ticket with setting up the environment.

## Dependencies

- **workspace** (required) — provides worktree creation, setup, and dev servers. **Load `/t3:workspace` now** if not already loaded.

## Workflow

### 1. Fetch Issue Context

#### One URL Is Enough (Non-Negotiable)

When the user pastes **any single URL** — Notion page, GitLab/GitHub issue/MR/PR, Slack message — that is the **starting point**, not the complete brief. Traverse outward exhaustively until every reachable artifact has been gathered. Do not ask the user for more URLs that you could have followed from the one they gave you.

The user should be able to type "review <one URL>" and have the agent assemble:

- the page/issue body
- every comment, discussion, inline thread, and resolved discussion
- every linked sub-page, child Notion page, attached PDF, attached image
- every linked GitLab/GitHub issue or MR/PR — recursively (an issue's linked MRs link back to comments worth reading)
- every linked Slack thread (parent message + all replies)
- the ticket's referenced spec attachments — never skip a PDF just because it's hard to download

Asking the user for additional URLs they've already cross-linked is a retro-worthy failure.

#### Per-Platform Traversal

| Starting URL | What to traverse |
|---|---|
| **Notion page** | Page text with `include_discussions=true`. Every `<page-discussions>` entry. Every embedded image. Every `<file>` attachment → download with `t3 tool notion-download` (signed URL from a Brave click). The `GitLab Reference` / `Source` / `Linked Issue` properties → fetch that issue too. Any `notion.so/<id>` links inside the body → fetch each as a child. Slack `archives/.../p<ts>` mentions → fetch that thread. |
| **GitLab issue / MR** | Body, `/discussions`, `/notes` (paginated), `related_merge_requests` for issues — fetch every related MR's discussions too. Author + assignees. Description-embedded URLs (Notion, Slack, sibling MRs). Any uploaded files (`uploads/<secret>/<file>`). |
| **GitHub issue / PR** | Body, all comments, review comments, linked PRs/issues, every embedded image. |
| **Slack message link** (`archives/<channel>/p<ts>`) | The thread parent + all replies via `slack_read_thread`. Any mentioned tickets / Notion pages in the thread → recurse. |

#### Tooling Rules

- **CLI over MCP** — use `glab`, `gh`, `t3 tool notion-download` first; reach for MCP only for services without a CLI.
- **Image safety** — before reading any downloaded image, validate it with `file <path>`. Only raster (PNG/JPEG/GIF/WebP) are safe to read. Non-raster (SVG/XML/HTML) or empty/corrupt files will poison the conversation context with unrecoverable "Could not process image" errors.
- **Pagination** — `glab api .../notes` returns one page (typically 20). Use `?per_page=100` or `--paginate` and de-duplicate.
- **Inaccessible sources** — if a link points to a source you cannot reach (e.g., partner Jira behind SSO), STOP and report it. Do not silently proceed with a partial picture.

#### Stop Conditions

Stop traversing only when:

- Every link encountered has been visited or explicitly classified as out-of-scope (e.g., the overlay declares a host as inaccessible).
- The signal-to-noise ratio of further fetches is clearly low (e.g., a 200-comment thread where the last 50 are reactions/emojis).

If you stop before exhausting the reachable graph, **explicitly tell the user what you skipped and why**.

### 1b. Check For Resolved-But-Open Issues

Before treating the issue as work to do, check whether a merged MR/PR has already shipped it. Squash-merges that name the issue as `(#N)` rather than `Closes #N` leave the issue `OPEN` even though the work is done — the pipeline will keep scheduling phases against it.

```bash
gh pr list --repo <owner>/<repo> --search "in:title #<issue-number>" --state merged --json number,title,mergedAt
# or for GitLab:
glab mr list --search "#<issue-number>" --state merged
```

If a merged PR references this issue and its body claims the work is complete, **stop and confirm with the user** before continuing. If the user agrees the work is done, close the issue with a comment pointing to the merged PR — do not start a redundant scoping/implementation pass.

**Run this check even when an upstream brief, coordinator, or mission prompt names the ticket as the "current" or "next" one.** A brief asserting a ticket authoritatively is not evidence the ticket is unresolved — backlogs drift and merged-but-open issues accumulate. Verify against merged PRs *before* creating a worktree, not after. Closing the stale issue with evidence and advancing to the next backlog item is the correct outcome, not a deviation from the brief.

### 2. State Acceptance Criteria

- Extract and list acceptance criteria before coding.
- If the ticket is vague, clarify with the user.

### 2b. Infer Deliverables

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

### 5. Detect Variant/Tenant (Multi-Tenant Projects)

**Always detect the target tenant before coding.** This determines environment setup, feature flag scope, and config repos.

1. **Check issue labels** — customer-name labels are authoritative, use directly.
2. **Check issue description** — explicit customer mentions or config-repo references.
3. **Check external tracker** — extract linked URLs from the issue description, fetch via MCP/CLI, look for customer/tenant properties. See project-specific skill references for the customer-name-to-variant mapping.
4. **Ask the user** — last resort, if none of the above yields a customer.

Pass the detected tenant to `t3 <overlay> worktree provision <customer>` and `t3 <overlay> worktree start <customer>`.

### 6. Create Worktree + Setup (Always — Don't Ask)

Worktree creation is the default for every ticket. **Never ask "should I create a worktree?"** — just do it after scope is confirmed.

Delegate to `/t3:workspace`:

- `t3 <overlay> workspace ticket` — create worktrees for affected repos.
- `t3 <overlay> worktree provision` — provision environment (symlinks, env, DB, direnv).

### 7. Start Dev Servers

Delegate to `/t3:workspace`:

- `t3 <overlay> worktree start` (brings the whole compose stack up — backend, sidecars, nginx-served frontend) or `t3 <overlay> run backend` / `t3 <overlay> run build-frontend` for targeted restarts.
- Verify services are running before declaring ready.

## Agent Rules

### User Hints Are Priority 1

When the user gives a debugging hint, **investigate that hint FIRST** before other theories. See `debug/SKILL.md § Phase 0: User Hints` for the full protocol.

### Parallel Agent Dispatch

- **Use when:** 2+ independent problem domains, independent research, independent file modifications
- **Don't use when:** failures might be related, changes to shared state, need full system context first
- **Post-parallel:** review all summaries, check for conflicts, run full verification
