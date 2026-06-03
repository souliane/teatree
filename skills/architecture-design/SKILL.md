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

Generic planning methodology is delegated to `obra/superpowers/writing-plans`. The teatree-specific value-add is the nine-check architecture pass below, plus the `ARCHITECTURE.md` template the implementer fills in before touching `src/`.

## When the gate fires

The nine checks apply when the work meets any of:

- touches `src/teatree/cli/`, `src/teatree/core/`, `src/teatree/loop/scanners/`, `src/teatree/agents/`, `OverlayBase`, scanner registration, or any `*Backend` Protocol
- crosses an FSM phase boundary (introduces or moves a `Ticket.State` transition)
- introduces a new module under `src/teatree/`
- changes a Protocol surface or an entry-point contract
- changes BLUEPRINT.md or any `docs/blueprint/*.md` appendix

Tactical fixes (typo, narrow string change, single-call-site bug) skip the gate — the implementer notes that in the PR body.

## The nine checks

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

### 8. Identity and key normalization

When a logical identity has both a bare and a qualified form (namespace:name, scope/path, prefix+id, fully-qualified name), the **fully-qualified form is the canonical key**.

- **One source-of-truth function** normalizes every reference UP to the fully-qualified form at every boundary — read, write, compare, dedup, cache lookup, registry lookup.
- Stripping a qualifier to make two things match discards qualifying information and conflates genuinely distinct entities (e.g. `t3:review` vs `overlay-a:review`), creating silent collisions that produce wrong results with no error.
- **Normalization smell to challenge at review:** any `split(":")[-1]`, `rsplit("/", 1)[-1]`, `removeprefix(...)`, or `lstrip(prefix)` whose sole purpose is to make a comparison succeed. The under-qualified side should be canonicalized UP instead. A transformation that can only ever lose information is almost always the wrong seam.

_Example: skill name lookups in the registry. The registered key is `namespace:skill-name`. A lookup arriving as bare `skill-name` should be qualified to `namespace:skill-name` at the boundary, not matched by stripping the namespace off the registered key._

### 9. Behavior preservation / capability deletion

The other eight checks cover behavior the change INTRODUCES. This one covers behavior it REMOVES — the high-deletion "replace an existing implementation" class where regressions hide.

For any change that replaces or rewrites an existing implementation, **enumerate every behavior/case the old code handled and justify each one you drop**. Use `git show <base>:<path>` to read the pre-change behavior; do not rely on memory.

- A narrowing of a **privacy / leak / security matcher** — or of the gate's coverage in general — requires explicit user sign-off. A unilateral "documented trade-off" that weakens a public-repo privacy gate is a BLOCKER, not a self-approve.
- **Never invert an existing must-block regression test to must-not-block** (e.g. `returncode == 1` → `== 0`) to make a weaker matcher pass. Deleting or inverting the test that pinned the old behavior is the tell that coverage was dropped without preservation.
- If a behavior genuinely must be dropped, the removal is its own reviewed decision: list it here, justify it, and (for safety gates) get sign-off — preserve-or-STOP, never silently narrow.

## Anti-pattern catalog

The nine checks above are the curated, narrative core. Their machine-checked superset is the anti-pattern catalog at [docs/generated/antipattern-catalog.md](../../docs/generated/antipattern-catalog.md) — generated from `src/teatree/quality/antipatterns.yaml`, the single source of truth. Each entry carries a detection tier (`greppable` or `judgement`) feeding the three review tiers: this design-time pass, the per-PR deterministic linter (`scripts/hooks/check_antipatterns.py`, manual stage), and the periodic holistic review in `ac-reviewing-codebase`. A reviewer skimming a design can use the catalog as the checklist the nine prose checks summarize.

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

## 8. Identity and key normalization
<identities with bare vs qualified forms; canonical form chosen; one normalization function at every boundary; any strip/split whose purpose is to make a comparison succeed — justify or remove>

## 9. Behavior preservation / capability deletion
<for a change that replaces/rewrites existing code: enumerate every behavior the old code handled, mark each preserved or dropped; flag any narrowing of a privacy/leak/security matcher as requiring user sign-off; confirm no must-block test was inverted to must-not-block; "n/a — purely additive" if nothing is removed>
```

## Workflow

1. Read `BLUEPRINT.md` and the appendix for the touched section.
2. Run the nine checks against the proposed change.
3. Write `ARCHITECTURE.md` in the worktree root.
4. Hand off to the implementation skill (`t3:code`) — it picks up from here with TDD.

## Delegation

- `obra/superpowers/writing-plans` — generic planning methodology (problem framing, alternatives considered, rollback path)
- `t3:code` — implementation phase, picks up after `ARCHITECTURE.md` is written
- `t3:ticket` § "Plan First" — the ticket-intake pre-check that triggers this companion
- `t3:review` § "North-Star Rubric — Six Quality Attributes" — the clean / robust / maintainable / coherent / reliable / proactive lens the resulting design is reviewed against (coherence covers the cross-repo and dependency-direction checks above)

## Scope discipline

This skill ships v1 with the nine checks above. If a check is missing from the in-worktree `ARCHITECTURE.md`, the reviewer surfaces it as a discussion thread on the PR — no merge until the gap is closed.

The companion does not block implementation skills from loading — it loads alongside them. The discipline is that the implementer reads it first; the PR review enforces that the artifact (`ARCHITECTURE.md`) was produced.
