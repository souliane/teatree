---
name: architecture-design
description: Architecture pre-check companion. Loaded transitively by implementation skills (code, ticket-for-features, retro-for-skill-changes) to force an architecture pass — BLUEPRINT alignment, FSM phase boundaries, extension-point contracts, component boundaries, dependency direction, test surface, resilience invariants, removability — BEFORE any code is written.
compatibility: macOS/Linux, any teatree-managed repo.
requires:
  - writing-plans
metadata:
  version: 0.0.1
---

# Architecture-Design Companion

Implementation skills (`t3:code`, `t3:ticket` for features, `t3:retro` for skill changes) `require` this skill, so it loads before any code is written. `writing-plans` carries the generic planning method; this skill adds the ten checks below. Draft them in a gitignored, worktree-local `ARCHITECTURE.md` and carry them into the PR body as `## Architecture pre-check`. The scope is THIS repo's conventions. Provider-level agent architecture is the optional vendor `claude-api` skill (`src/teatree/provisioning/recommended.py`).

## Standing bar — the project is in salvage mode

Every design, and every review of one, holds these before the ten checks:

1. **Net LoC is the first metric.** State the change's net line count; a line that does not earn its place is a finding.
2. **One concept, one place.** One generic primitive beats N bespoke copies (one `Gate` primitive, one parsed command rather than a regex per hook).
3. **Replace, never parallel.** A new capability replaces the path it supersedes. For a never-enabled feature, default to deleting its toggle and keeping the feature on, with a test that it runs. Decide per feature with evidence; remove the feature only if unsafe, destructive, unfinished, or superseded, and show the owner a per-feature decision table before deletion.
4. **Test through public entry points**, mocking only external boundaries. A mock-heavy unit test of internals is a finding.
5. **Prove it on the real surface** — an end-to-end run where the change acts — before merge. "Tests green" alone is not proof.

## When the gate fires

The ten checks apply when the work meets any of:

- touches `src/teatree/cli/`, `src/teatree/core/`, `src/teatree/loop/scanners/`, `src/teatree/agents/`, `OverlayBase`, scanner registration, or any `*Backend` Protocol
- crosses an FSM phase boundary (introduces or moves a `Ticket.State` transition)
- introduces a new module under `src/teatree/`
- changes a Protocol surface or an entry-point contract
- changes BLUEPRINT.md or any `docs/blueprint/*.md` appendix

Tactical fixes (typo, narrow string change, single-call-site bug) skip the gate — the implementer notes that in the PR body.

### Making an existing function stricter — search its callers first (Non-Negotiable)

A new liveness/strictness gate or a new `None`/`False` return on an existing function changes every caller, so it is "single-call-site" only once a search proves it. Your first command is the tree-wide search: it prints the definition and every caller in one pass, so opening the defining file first saves nothing. Then apply check 9 to each caller it finds.

```bash
git grep -n "<name>("               # do X first: the definition plus every caller, tree-wide
grep -n "<name>" <defining-file>    # never Y first: one file cannot show who else calls it
```

## The ten checks

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
- `hooks/scripts/` — Claude Code hook handlers (PreToolUse / PostToolUse / Stop / SessionStart)

If the new code straddles two categories, split it.

### 5. Dependency direction

Read the auto-generated dependency graph in [docs/dependency-graph.md](../../docs/dependency-graph.md) (linked from `BLUEPRINT.md` "Module Dependency Graph") before adding any import. The graph encodes the DAG enforced by tach.

A lower-level module (e.g. `teatree.utils`, `teatree.config`) MUST NOT import from a higher-level one (e.g. `teatree.cli`, `teatree.core.management`). A backwards edge is a refactor first, an implementation second — surface it on the PR and propose the inversion (callback, registration, Protocol) that breaks the edge.

After adding any new cross-module import, **verify the boundary DAG with `tach`, never by eyeballing it** — run `uv run tach check` to confirm the import introduced no backwards edge. `uv run tach check` reproduces the enforced gate locally; do not substitute an `echo "looks fine"` for the real check.

### 6. Test surface

For each behaviour the change introduces, name the test file and the assertion that would fail if it regressed, driven through the public entry point (bar 4). No test surface means no observable contract — restructure before coding. An FSM transition asserts the post-transition state plus the queued follow-up task; a scanner asserts what `scan()` emits; a hook handler asserts its decision.

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
- **Tightening a shared predicate changes every caller, not only the one you wrote it for.** For each caller the tree-wide search finds, name the decision it makes. A release/commit caller may need the strict read; a park/defer caller does not, and inherits a wrong verdict when you tighten in place. When requirements differ, add a strict variant for the caller that needs it, leave the shared one as it was, and pin the untouched caller with a test (#4880: a release-path liveness gate turned a park into a HALT).
- If a behavior genuinely must be dropped, the removal is its own reviewed decision: list it here, justify it, and (for safety gates) get sign-off — preserve-or-STOP, never silently narrow.

### 10. Removability / harness-vs-data

Two questions every new component must answer.

- **Removability.** State the blast radius of deleting it. A self-contained module is removable; one whose deletion forces edits across N call sites cannot be cheaply reverted when it proves wrong — prefer the shape that can be pulled out.
- **Harness vs data.** A per-item policy a table can express goes in data (a row keyed by a real domain entity), with the harness as the generic mechanism reading it. **A setting is not the free answer to this question.** "In data" means a table or registry row keyed by a real domain entity; a new toggle is surface paid again at every reader, and one whose correct value is computable from something already known is a bug with a knob on it. `architectural-review` § 3.17 "Minimal Configurable & Pluggable Surface" asks the same question at review time — answer it here so the two passes agree. Justify the side you chose.

The deterministic validator `teatree.quality.architecture_precheck` warns at PR creation (`core.gates.architecture_precheck_gate`, both PR chokepoints) when the pre-check leaves this check absent or a bare placeholder.

## Anti-pattern catalog

The machine-checked superset of these checks is [docs/generated/antipattern-catalog.md](../../docs/generated/antipattern-catalog.md), generated from `src/teatree/quality/antipatterns.yaml`. Its `greppable` entries feed the per-PR linter (`scripts/hooks/check_antipatterns.py`); its `judgement` entries feed this design pass and the periodic `architectural-review`.

## Architecture pre-check template

Fill it BEFORE touching `src/`. The scratch file is never committed, so it cannot conflict across tickets; the PR body carries the same sections.

```markdown
## Architecture pre-check — <ticket-ref>

Net LoC: <+added / -removed = net>, and every line that did not earn its place

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
<for a change that replaces/rewrites existing code: enumerate every behavior the old code handled, mark each preserved or dropped; flag any narrowing of a privacy/leak/security matcher as requiring user sign-off; confirm no must-block test was inverted to must-not-block; for a tightened shared predicate: every caller and its decision, split strict/lenient where they differ; "n/a — purely additive" if nothing is removed>

## 10. Removability / harness-vs-data
<is the component removable — the blast radius of deleting it; does it belong in the harness (code/gate/FSM) or in data/config (table/setting/registry)? justify the side chosen>
```

## Workflow

Read `BLUEPRINT.md` and the touched appendix, run the standing bar and the ten checks, draft the pre-check, then hand off to `t3:code` for TDD. `t3:review` § "North-Star Rubric" judges the result.

## Scope discipline

A pre-check missing from the PR body, or leaving a check unanswered, is a review thread that blocks merge. `core.gates.architecture_precheck_gate` backs check 10 with a warn-only signal at PR creation. The companion loads alongside implementation skills and never blocks them loading.
