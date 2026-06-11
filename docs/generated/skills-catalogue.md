# Skills Catalogue

Source: `skills/*/SKILL.md` frontmatter

| Skill | Summary |
| --- | --- |
| `answerer` | Draft a reply to an inbound question, DM the user for approval, post on confirmation |
| `architecture-design` | Architecture pre-check companion. Loaded transitively by implementation skills (code, ticket-for-features, retro-for-skill-changes) to force an architecture pass — BLUEPRINT alignment, FSM phase boundaries, extension-point contracts, component boundaries, dependency direction, test surface, resilience invariants — BEFORE any code is written |
| `availability` | 24/7 dual question-mode — switch between asking the user now (present) and capturing questions as durable `DeferredQuestion` rows (away) |
| `checking` | A SHORT "what did I miss" report when the user checks in mid-loop — terse, grouped, clickable; then answer the pending deferred questions in-band |
| `code` | Writing code with TDD methodology |
| `contribute` | Push retro improvements to a branch, open a PR, and optionally create upstream issues |
| `debug` | Troubleshooting and fixing — something is broken, find and fix it |
| `e2e` | End-to-end testing with Playwright — writing tests, running them, visual snapshots, evidence posting, and the pre-push visual QA gate |
| `e2e-review` | Reviewer-side quality gate for frontend Playwright E2E specs — business-readable scenario names, stable selector contracts, condition-not-clock waits, one-behaviour-per-test, fixture/cleanup discipline, no hardcoded creds/URLs, POM patterns — plus the procedure for converting an externally-authored suite into a project's conventions |
| `followup` | Daily follow-up — batch process new tickets, check/advance ticket statuses, remind about PRs waiting for review |
| `handover` | Use when the user wants to hand all current work from one Claude session to another (or to a not-yet-existing session) with a single command, or to transfer an in-flight TeaTree task from Claude to another runtime, or asks whether it is time to switch because Claude usage is getting high |
| `loops` | Show t3 loop status — which loops are running vs stalled, the cadence and next tick of each loop, and loop ownership |
| `next` | Wrap up the current session — retro, structured result, pipeline handoff |
| `platforms` | Platform-specific API recipes for GitLab, GitHub, Slack, and X (Twitter). Auto-loaded as a dependency by skills that interact with these platforms |
| `retro` | Conversation retrospective and skill improvement |
| `review` | Code review — self-review before finalization, giving review, receiving review feedback |
| `review-request` | Batch review requests — discover open PRs, validate metadata, check for duplicates, post to review channels |
| `rules` | Cross-cutting agent safety rules — clickable refs, temp files, sub-agent limits, UX preservation. Auto-loaded as a dependency by other skills |
| `running-evals` | Single in-session entrypoint that auto-orchestrates the whole eval picture — free deterministic lanes (skill-triggers, pinned-regressions) plus the subscription AI/trajectory lane (prepare → produce transcripts in-session → grade) — and prints one unified results table |
| `scanning-news` | Scans today's TLDR AI and The Rundown AI editions for ideas that could improve teatree, fetches the full article for promising items, queues each concrete t3-improvement candidate behind an ask-gate (PendingArticleSuggestion) for per-article user approval before any souliane/teatree issue is filed, and posts a terse Slack DM summary |
| `setup` | Bootstrap and validate teatree for local use — prerequisites, config, skill symlinks, optional agent hooks, and Django project scaffolding |
| `ship` | Delivery — committing, pushing, creating MR/PR, pipeline monitoring, review requests |
| `speed` | The parallel-work throughput dial — slow / medium / full / boost. `boost` runs one parallel-backlog-blast wave; `full` arms a self-sustaining boost loop; `medium` (baseline) and `slow` cap concurrency |
| `sweeping-prs` | Maintenance sweep across all your open PRs/PRs — merge the default branch, fix conflicts, monitor CI, push, and (per-repo policy) optionally squash-merge each PR before moving to the next. Never rebases |
| `teatree` | TeaTree agent lifecycle platform — core architecture, lifecycle phases, CLI reference, overlay API, skill loading, and plugin hooks |
| `teatree-batch` | Unattended batch ticket processing — work through a prioritized backlog one ticket at a time, sequentially. Create worktree, implement with TDD, self-review, push, merge, clean up. Skip tickets that need design decisions |
| `teatree-bughunt` | Self-QA variant of batch mode — dogfood the teatree loop and statusline, find real bugs (missing signals, broken links, stale data, scanner errors), file them, then fix them in worktrees |
| `teatree-dogfood` | Dogfooding checklist for teatree CLI, loop, and statusline changes — verify fresh behavior by running the command yourself, exercising the full task lifecycle, and watching the rendered statusline before declaring a change done. Also lists the known worktree/uv/git-stash pitfalls that trip up local validation |
| `teatree-plan` | Backlog prioritization with the GitHub Projects v2 board as single source of truth. Syncs repo issues to the board, walks the user through prioritization one question at a time, and reorders/updates board columns |
| `test` | Testing, QA, and CI — running tests, analyzing failures, quality checks, CI interaction, test plans, and posting testing evidence |
| `ticket` | Ticket intake and kickoff — from zero to ready-to-code |
| `todos` | List the current session's tasks/todos — terse, grouped pending / in_progress / completed, with clickable refs |
| `update` | WHEN to bring teatree core and registered overlays up to date with their default branch, and the safety guarantees of doing so |
| `workspace` | Environment and workspace lifecycle — worktree creation, setup, DB provisioning, dev servers, cleanup |
