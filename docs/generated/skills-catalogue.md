# Skills Catalogue

Source: `skills/*/SKILL.md` frontmatter

| Skill | Summary |
| --- | --- |
| `answerer` | Draft a reply to an inbound question, DM the user for approval, post on confirmation |
| `architecture-design` | Architecture pre-check companion. Loaded transitively by implementation skills (code, ticket-for-features, retro-for-skill-changes) to force an architecture pass — BLUEPRINT alignment, FSM phase boundaries, extension-point contracts, component boundaries, dependency direction, test surface, resilience invariants, removability — BEFORE any code is written |
| `availability` | 24/7 dual question-mode — switch between asking the user now (present) and capturing questions as durable `DeferredQuestion` rows (away) |
| `checking` | A SHORT "what did I miss" report when the user checks in mid-loop — terse, grouped, clickable; then answer the pending deferred questions in-band |
| `cleanup-sweep` | Use when sweeping stale, lost, or abandoned worktrees, branches, or stashes that are NOT actively being worked — deciding per item whether to salvage unmerged work to a fresh PR, delete a shipped/superseded/redundant item, push post-merge commits to a new PR, or keep an uncertain one. The judgment layer over `t3 <overlay> workspace emit` / `salvage` / `clean-all` (the mechanical reaper is `/t3:workspace`) |
| `code` | Writing code with TDD methodology |
| `contribute` | Push retro improvements to a branch, open a PR, and optionally create upstream issues |
| `debug` | Troubleshooting and fixing — something is broken, find and fix it |
| `dogfooding-teatree` | Dogfooding teatree's own CLI, loop, and statusline — two modes sharing one mechanics section for reading a tick and the rendered statusline. "Verify a change" is the run-it-yourself checklist applied after modifying CLI/loop/statusline code, before declaring it done. "Hunt for bugs" is proactive self-QA — dogfood the deployed loop, find/dedupe/confirm real bugs, file them, then fix them in worktrees |
| `dreaming` | Runs the idle-time "dreaming" memory-consolidation pipeline end to end with one command — replay recent transcripts + curated memories, distil drift into the ConsolidatedMemory ledger, cross-link / re-index / decay the memory files, run the §4 acceptance gates, triage each row into keep-as-memory vs core-gap → drive each core gap to a MERGED fix under the standing umbrella issue, and promote/stage eval candidates |
| `e2e` | End-to-end testing with Playwright — writing tests, running them, visual snapshots, test-plan posting, and the pre-push visual QA gate |
| `e2e-review` | Reviewer-side quality gate for Playwright end-to-end specs. Load when reviewing a new or changed E2E test, deciding whether a spec is ready to land, or adopting an outside Playwright suite. Judges specs against Playwright's published best practices — user-visible behaviour over implementation, resilient role/label/test-id locators, web-first auto-retrying assertions instead of hard waits, per-test isolation, page-object structure, and runnable evidence — and tells the implementer what to fix before approval |
| `followup` | Daily follow-up — batch process new tickets, check/advance ticket statuses, remind about PRs waiting for review |
| `handover` | Use when the user wants to hand all current work from one Claude session to another (or to a not-yet-existing session) with a single command, or to transfer an in-flight TeaTree task from Claude to another runtime, or asks whether it is time to switch because Claude usage is getting high |
| `loops` | Show t3 loop status and trigger DB-configured loops — which loops are running vs stalled, the cadence/next-tick of each, loop ownership, and how to trigger a per-loop tick |
| `next` | Wrap up the current session — retro, structured result, pipeline handoff |
| `platforms` | Platform-specific API recipes for GitLab, GitHub, Slack, and X (Twitter). Auto-loaded as a dependency by skills that interact with these platforms |
| `prompts` | Trigger and manage reusable prompts — list the prompts in the DB, render one by name with its templated params, and point to the admin for authoring + version history |
| `retro` | Conversation retrospective and skill improvement |
| `review` | Code review — self-review before finalization, giving review, receiving review feedback |
| `review-request` | Batch review requests — discover open PRs, validate metadata, check for duplicates, post to review channels |
| `rules` | Cross-cutting agent safety rules — clickable refs, temp files, sub-agent limits, UX preservation. Auto-loaded as a dependency by other skills |
| `running-evals` | Single in-session entrypoint that auto-orchestrates the whole eval picture — free deterministic lanes (skill-triggers, the eval-coverage gate `t3 eval coverage`, pinned-regressions) plus the transcript AI/trajectory lane (prepare → produce transcripts in-session → grade) — and prints one unified results table |
| `scanning-news` | Scans today's TLDR AI and The Rundown AI editions for ideas that could improve teatree, fetches the full article for promising items, queues each concrete t3-improvement candidate behind an ask-gate (PendingArticleSuggestion) for per-article user approval before any souliane/teatree issue is filed, and posts a terse Slack DM summary |
| `setup` | Bootstrap and validate teatree for local use — prerequisites, config, skill symlinks, optional agent hooks, and Django project scaffolding |
| `ship` | Delivery — committing, pushing, creating MR/PR, pipeline monitoring, review requests |
| `sweeping-prs` | Maintenance sweep across all your open PRs/PRs — merge the default branch, fix conflicts, monitor CI, push, and (per-repo policy) optionally squash-merge each PR before moving to the next. Never rebases |
| `sweeping-tickets` | Evidence-gated ticket/issue consolidation and triage — classify every open issue against current `main`, then consolidate by merging related tickets into a small set of tracking epics (never by discarding ideas) and close only what is demonstrably shipped or now folded into an epic. Always asks the operator for the maximum number of tickets/epics to keep before triaging — never assumes a number. Dry-run first; close only on user approval (or auto-close ONLY the high-confidence "shipped by merged PR #X" class), posting a one-line reason on every close |
| `teatree` | TeaTree agent lifecycle platform — core architecture, lifecycle phases, CLI reference, overlay API, skill loading, and plugin hooks |
| `teatree-batch` | Unattended batch ticket processing — work through a prioritized backlog one ticket at a time, sequentially. Create worktree, implement with TDD, self-review, push, merge, clean up. Skip tickets that need design decisions |
| `test` | Testing, QA, and CI — running tests, analyzing failures, quality checks, CI interaction, test plans, and posting testing evidence |
| `ticket` | Ticket intake and kickoff — from zero to ready-to-code |
| `todos` | List the current session's tasks/todos — terse, grouped pending / in_progress / completed, with clickable refs |
| `update` | WHEN to bring teatree core and registered overlays up to date with their default branch, and the safety guarantees of doing so |
| `wip` | The bounded-WIP throughput dial — slow / medium / full / boost. `boost` runs one parallel-backlog-blast wave; `full` arms a self-sustaining boost loop; `medium` (baseline) and `slow` cap concurrency |
| `workspace` | Environment and workspace lifecycle — worktree creation, setup, DB provisioning, dev servers, cleanup |
