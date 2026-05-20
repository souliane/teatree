---
name: architecture-design
description: Architecture pre-check companion. Loaded transitively by implementation skills (code, ticket-for-features, retro-for-skill-changes) to force an architecture pass — BLUEPRINT alignment, FSM phase boundaries, extension-point contracts, component boundaries, dependency direction, test surface, resilience invariants — BEFORE any code is written.
compatibility: macOS/Linux, any teatree-managed repo.
companions:
  - writing-plans
metadata:
  version: 0.0.1
  subagent_safe: true
---

# Architecture-Design Companion

## Why this loads automatically

This is a companion gate. Implementation skills (`t3:code`, `t3:ticket` for new features, `t3:retro` when a retro touches skills) declare `requires: [architecture-design]` so it loads BEFORE coding starts. The flywheel (BLUEPRINT § 17.1 invariant 2) is enforcement encoded as structure, not prose vigilance — this companion is the structure for the "design first, then code" step.

Generic planning methodology is delegated to `obra/superpowers/writing-plans`. The teatree-specific value-add is the seven-check architecture pass below, plus the `ARCHITECTURE.md` template the implementer fills in before touching `src/`.

## When the gate fires

The seven checks apply when the work meets any of:

- touches `src/teatree/cli/`, `src/teatree/core/`, `src/teatree/loop/scanners/`, `src/teatree/agents/`, `OverlayBase`, scanner registration, or any `*Backend` Protocol
- crosses an FSM phase boundary (introduces or moves a `Ticket.State` transition)
- introduces a new module under `src/teatree/`
- changes a Protocol surface or an entry-point contract
- changes BLUEPRINT.md or any `docs/blueprint/*.md` appendix

Tactical fixes (typo, narrow string change, single-call-site bug) skip the gate — the implementer notes that in the PR body.

## The seven checks

### 1. BLUEPRINT § alignment

Cite the BLUEPRINT section the work touches (e.g. `§5.6 Loop Topology`, `§6 Overlay System`, `§17.4 Orchestrator-decides / loop-executes`). If no section is a clean fit, the work likely belongs to a new section — draft it in the same PR.

If the work contradicts the cited section, the BLUEPRINT change is part of the same PR (per CLAUDE.md "Documentation alignment"). Never let code and BLUEPRINT drift.

### 2. FSM phase boundaries

If the change involves `Ticket.State` or `Worktree.State` transitions, list the phases the work crosses (e.g. `coding → testing`, `reviewing → shipping`). For each crossing, name the transition method and the FSM condition it depends on.

If the change adds a new phase, it touches §4 (Domain Models) and §17.1 invariant 8 (FSM transitions via `t3` CLI) — both go in the BLUEPRINT update.

### 3. Extension-point contracts

If the change touches `OverlayBase`, a scanner registration, a hook surface, or a `*Backend` Protocol, list every overlay/scanner/hook that consumes the contract. Use `git grep -l 'OverlayBase\|register_scanner\|hook_router\|MessagingBackend\|CodeHostBackend'` as the floor, not the ceiling.

A Protocol/ABC change without a corresponding overlay-contract regression test is incomplete — the test surface check (#6) catches that.

### 4. Component boundaries

Justify the module choice. The recurring categories:

- `src/teatree/cli/` — typer commands, argparse, no business logic
- `src/teatree/core/` — Django models, managers, signals, transitions, services
- `src/teatree/loop/` — tick body, scanners, dispatcher
- `src/teatree/agents/` — sub-agent dispatch, skill bundles, prompt building
- `src/teatree/backends/` — Protocol implementations (Slack, GitHub, GitLab)
- `hooks/scripts/` — Claude Code hook handlers (PreToolUse / UserPromptSubmit / Stop / SessionStart)

If the new code straddles two categories, split it.

### 5. Dependency direction

Read `BLUEPRINT.md` "Module Dependency Graph" (the mermaid block) before adding any import. The graph encodes the DAG enforced by tach.

A lower-level module (e.g. `teatree.utils`, `teatree.config`) MUST NOT import from a higher-level one (e.g. `teatree.cli`, `teatree.core.management`). A backwards edge is a refactor first, an implementation second — surface it on the PR and propose the inversion (callback, registration, Protocol) that breaks the edge.

`uv run tach check` reproduces the gate locally.

### 6. Test surface

For each behaviour the change introduces, name the test file and the assertion that would fail if the behaviour regressed. A design that has no test surface is a design with no observable contract — restructure for observability before coding.

For FSM transitions, the test asserts the post-transition state plus the queued follow-up task (BLUEPRINT §4 invariant: state change + `transaction.on_commit(enqueue)` is atomic).

For scanners, the test calls `scan()` against a fixture and asserts the emitted events.

For hook handlers, the test calls the handler with a hook payload and asserts the decision (`allow` / `deny` / `additional_context`).

### 7. Resilience invariants (#1192)

For any external write (Slack post, GitHub PR mutation, GitLab MR update, DB row outside the request cycle, fs write under a watched path), verify the five invariants from #1192:

- **verify-by-re-read** — after the write, fetch the live state and confirm the mutation landed
- **fallback-transport** — when the primary channel is unavailable, the change has a sanctioned secondary path (durable DB row, snapshot, deferred task)
- **idempotency** — repeated invocation with the same input is a no-op, not a duplicate
- **heartbeat** — long-running work emits progress so a watchdog can distinguish "stuck" from "still working"
- **sub-agent return contract** — sub-agent results are structured (`StructuredResult`), not free-form prose; the orchestrator can route on them

If even one is missing, the design is incomplete — adding it later is a tech-debt commitment, not a follow-up.

## ARCHITECTURE.md template

The implementer drops a file at `ARCHITECTURE.md` in the worktree root BEFORE touching `src/`. The PR template references it; an empty or missing file is a review gap.

```markdown
# Architecture pre-check — <ticket-ref>

## 1. BLUEPRINT § alignment
<cite section, paste the one-line claim the work makes>

## 2. FSM phase boundaries
<phases crossed, transition methods, FSM conditions; "n/a" if no transition>

## 3. Extension-point contracts
<every OverlayBase / scanner / hook / Protocol consumer affected>

## 4. Component boundaries
<module chosen + justification; if straddling, the split>

## 5. Dependency direction
<imports added; confirm no backwards edge; `uv run tach check` output>

## 6. Test surface
<test file + assertion per behaviour; FSM/scanner/hook specifics>

## 7. Resilience invariants
<per external write: verify-by-re-read, fallback-transport, idempotency, heartbeat, sub-agent return contract>
```

## Workflow

1. Read `BLUEPRINT.md` and the appendix for the touched section.
2. Run the seven checks against the proposed change.
3. Write `ARCHITECTURE.md` in the worktree root.
4. Hand off to the implementation skill (`t3:code`) — it picks up from here with TDD.

## Delegation

- `obra/superpowers/writing-plans` — generic planning methodology (problem framing, alternatives considered, rollback path)
- `t3:code` — implementation phase, picks up after `ARCHITECTURE.md` is written
- `t3:ticket` § "Plan First" — the ticket-intake pre-check that triggers this companion

## Scope discipline

This skill ships v1 with the seven checks above. If a check is missing from the in-worktree `ARCHITECTURE.md`, the reviewer surfaces it as a discussion thread on the PR — no merge until the gap is closed.

The companion does not block implementation skills from loading — it loads alongside them. The discipline is that the implementer reads it first; the PR review enforces that the artifact (`ARCHITECTURE.md`) was produced.
