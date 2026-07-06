---
name: ticket
description: Ticket intake and kickoff — from zero to ready-to-code. Use when user says "I have ticket X", "new ticket", "start working on", "what should I do for this?", or provides a ticket/issue/PR link.
compatibility: macOS/Linux, zsh or bash, git, glab or gh CLI for issue fetching.
requires:
  - workspace
  - architecture-design
  - writing-plans
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

#### One Ticket At A Time — Never Conflate (Non-Negotiable)

Under load — a polluted prior-session context, a coordinator brief, several tickets discussed in one breath — the failure is to fetch the **wrong** ticket, or to blend two tickets' details into one. Do this, never that:

1. **Fetch exactly the id in the live request.** The ticket to act on is the one in the user's *current* message, not the one a stale brief, handover, or earlier turn named as "current"/"next". When in doubt, fetch the id the user just typed — never a remembered one.
2. **Pin the id before the fetch.** State the single `<repo>#<iid>` (or full URL) you are about to fetch, then run the one `glab issue view` / `gh issue view` command above against exactly that id.
3. **Never merge two tickets' context.** If the session has touched ticket A and the user now hands you ticket B, fetch B fresh and keep A's acceptance criteria, scope, and tenant out of B. Lessons carry; details do not.
4. **One fetch, one ticket, then stop and read it.** Do not batch-fetch a menu of ids you "might" need; fetch the requested one and act on it.

Acting on a different ticket than the one requested — or smearing one ticket's spec onto another — is a retro-worthy failure even when an upstream brief made the conflation tempting.

#### Per-Platform Traversal

| Starting URL | What to traverse |
|---|---|
| **Notion page** | Page text with `include_discussions=true`. Every `<page-discussions>` entry. Every embedded image. Every `<file>` attachment → download with `t3 tool notion-download` (signed URL from a Brave click). The `GitLab Reference` / `Source` / `Linked Issue` properties → fetch that issue too. Any `notion.so/<id>` links inside the body → fetch each as a child. Slack `archives/.../p<ts>` mentions → fetch that thread. |
| **GitLab issue / MR** | Body, `/discussions`, `/notes` (paginated), `related_merge_requests` for issues — fetch every related MR's discussions too. Author + assignees. Description-embedded URLs (Notion, Slack, sibling MRs). Any uploaded files (`uploads/<secret>/<file>`). |
| **GitHub issue / PR** | Body, all comments, review comments, linked PRs/issues, every embedded image. |
| **Slack message link** (`archives/<channel>/p<ts>`) | The thread parent + all replies via `slack_read_thread`. Any mentioned tickets / Notion pages in the thread → recurse. |

#### Tooling Rules

- **CLI over MCP (do this — never the reverse).** Fetch the issue body with the forge CLI as the **first** action. Reach for an MCP service only for a source that has no CLI (e.g. Notion, Slack). Do **not** open an MCP issue-fetch when `glab`/`gh` can read the same issue.

  The one command to run for the URL the user pasted — copy-paste, fill the placeholders, no narration:

  ```bash
  # GitLab issue (gitlab.com/<group>/<repo>/-/issues/<iid>) — body + every discussion in one pass
  glab issue view <iid> --repo <group>/<repo> --comments

  # GitHub issue (github.com/<owner>/<repo>/issues/<n>) — body + all comments
  gh issue view <n> --repo <owner>/<repo> --comments

  # Notion / Slack / other CLI-less source only
  t3 tool notion-download <signed-url>
  ```

  Then traverse the linked graph (sub-pages, related MRs, threads) per the table above. `glab issue view ... --comments` / `gh issue view ... --comments` is the canonical intake fetch — not an MCP call.
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

### 1c. Landscape Survey (Non-Negotiable — feeds the planner)

§1b checks only *this* issue against merged PRs. The **landscape survey** is the wider intake duty: before any plan is designed, survey what is **already in flight or already settled** across the repos in scope, then hand the result to the planner so it plans *against* reality instead of re-deriving it. This is intake's job, **not** the planner's — the planner consumes the survey, it does not run it.

The survey has four parts:

1. **Enumerate the open work** — list every open ticket/issue, MR, and PR for the repos in scope (not just the one ticket).
2. **Classify each open ticket/issue** — `done` (a merged PR shipped it, issue still open), `partially done` (an open PR/branch carries the work), `deprecated` / `superseded` (a newer ticket or merged change replaces it), `won't-do` (out of scope / explicitly declined), or genuinely `open`.
3. **Inspect local work-in-flight** — unpushed commits, existing worktrees, and open MRs/PRs that mean work has *already started* for this (or an overlapping) ticket. Starting fresh on top of a forgotten branch is the failure this prevents.
4. **Emit recommendations** — for each open ticket, a concrete suggested action (`close` citing the merged PR, `merge`/finish the existing PR, `supersede` by the named sibling, or `keep` and plan). These recommendations are the survey's deliverable; surface them to the user for any close/merge/supersede before acting, and pass the whole survey to the planner.

**Run the survey through the CLI — it gathers the git + forge landscape deterministically so you do not hand-roll fragile `git`/`gh`/`glab` invocations:**

```bash
# The intake landscape: open PRs/MRs, local worktrees, unpushed commits, open issues,
# and a per-issue close/merge/supersede recommendation — emitted as structured output.
t3 <overlay> workspace landscape
```

When no CLI is available for a step, the raw probes the survey automates are:

```bash
gh pr list   --repo <owner>/<repo> --state open --json number,title,url,headRefName     # open PRs
glab mr list --repo <group>/<repo> --state opened                                        # open MRs
gh issue list --repo <owner>/<repo> --state open --json number,title,url                  # open issues
git worktree list --porcelain                                                             # local worktrees
git -C <worktree> status --porcelain                                                      # uncommitted work
git -C <worktree> log <branch> --not --remotes --oneline                                  # unpushed commits
```

The survey **fails open**: an inconclusive git probe or a forge that cannot be listed is reported as a warning, never silently dropped — a missed in-flight branch is worse than a noisy warning. The deterministic gather + classification lives in `teatree.core.intake.landscape` (`survey_landscape`); the planner receives the resulting `LandscapeSurvey` (open PRs, in-flight worktrees, per-issue recommendations) as input and **must not re-derive it**.

**Baked into the intake FSM step (#2541).** For the autonomous flow the survey is not a manual CLI step — the intake FSM worker (`execute_provision`, after the worktrees materialise and before the planner is scheduled) gathers it and persists a durable `LandscapeArtifact` row tied to the ticket. The planner then consumes that persisted survey (a headless planner sees it inline in its `INTAKE LANDSCAPE SURVEY` system-context block; any planner reads `LandscapeArtifact.latest_for(ticket)`), so the survey is *produced by intake and consumed by the planner via the FSM* rather than re-derived. Persistence is best-effort — a forge outage during provision never blocks provisioning or planning; the planner then falls back to the `t3 <overlay> workspace landscape` fetch above. `t3 <overlay> info artifacts <ticket>` surfaces the latest persisted survey alongside the ticket's other artifacts.

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
