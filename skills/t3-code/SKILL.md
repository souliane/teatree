---
name: t3-code
description: Writing code with TDD methodology. Use when user says "implement", "write", "add", "code", "feature", "build", or is actively coding a solution.
compatibility: macOS/Linux, any language/framework supported by the project.
requires:
  - t3-workspace
triggers:
  priority: 70
  keywords:
    - '\b(implement|code it|feature|refactor|rework|restructure|rewrite|redesign)\b'
    - '\b(fix|change|update|modify|adjust|add|remove|delete|write|create|build|move|rename|extract|split|merge|convert|migrate|optimize|improve|replace|swap|introduce|drop|deprecate|wire|hook up|integrate|extend|override|wrap|unwrap|inline|deduplicate|dedup|simplify|generalize|normalize|transform|adapt|port|backport|scaffold|stub|mock|patch|hotfix|tweak|rework|clean) (the|a|an|this|that|my|our|its|some|all|each|every)\b'
    - '^(fix|change|update|modify|adjust|add|remove|delete|write|create|build|move|rename|extract|refactor|replace|introduce|extend|override|simplify|optimize|improve|implement|convert|migrate|integrate|wire|hook|patch|hotfix|tweak|rework|clean up|scaffold|stub|mock|deduplicate|dedup) '
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Writing Code (TDD)

## Delegation

This skill delegates the generic implementation doctrine to:

- `test-driven-development` — red/green/refactor discipline and failing-test-first rules
- `verification-before-completion` — proof before any completion claim

TeaTree keeps the project-facing parts locally: worktree-aware setup via `t3-workspace`, feature-flag and tenant expectations, and the repo-specific verification gates.

## References

- [obra/superpowers](https://github.com/obra/superpowers) — TDD and development workflow skills

The implementation phase. Follow test-driven development and project conventions.

## Dependencies

- **t3-workspace** (required) — provides dev servers for live reload. **Load `/t3-workspace` now** if not already loaded.
- **Framework/language convention skills** (when backend is in scope) — e.g., Django conventions, Python style guides. TeaTree auto-detects the relevant `ac-*` skill from the repo shape. **If the loader didn't fire**, self-load the appropriate coding skill: `/ac-python` for Python code, `/ac-django` for Django projects. Load these **before writing any code**, not after.

## Workflow

### 1. Plan First

**Always make a plan before writing code.** Never jump straight to coding.

- Identify scope: which files, modules, and repos are affected.
- Review existing patterns in the codebase before writing new code.
- If the task matches a playbook, follow playbook-specific patterns.
- **Feature flag check:** Follow the decision gate in [`references/multi-tenant-development.md`](references/multi-tenant-development.md). Detect the target tenant using the priority chain from `t3-ticket § 6. Detect Variant/Tenant`.

**Scaling the plan to the task:**

- **Simple/clear tasks** (single file, obvious change): State the plan in a short bullet list, then start implementing immediately. No need for plan mode or user confirmation.
- **Complex/ambiguous tasks** (multi-file, architectural decisions, unclear scope): Use the agent's plan mode (if available) to block edits while planning. Explore the codebase, write the plan, present it for user approval. Only start coding after approval.
- **Config/discovery with multiple fallback sources** (settings resolution, env detection, overlay discovery): Map ALL user workflows in a table (who, how they install, what they need) BEFORE coding. One clean implementation beats 6 iterative patches.
- **Extracting overlay code to core** (generalization, refactoring): Write the BLUEPRINT spec first, then have the user review it before coding. Existing overlay code evolved organically — extracting it as-is copies its shortcuts. Design the clean-slate API from the spec, not from the existing implementation.

**How to decide:** If you would normally ask the user "is this approach okay?" before coding, that's a complex task — use plan mode.

### 2. TDD Cycle

```text
Write failing test → Implement → Green → Refactor
```

- **Red:** write a test that captures the expected behavior. Run it — must fail.
- **Green:** implement the minimum code to make the test pass.
- **Refactor:** clean up without changing behavior. Tests must stay green.

### 3. Follow Conventions

- Language/framework conventions from the project's convention skills (when loaded).
- Repository-specific patterns take precedence over generic guidance.
- Feature flag rules for new features (see [`references/multi-tenant-development.md`](references/multi-tenant-development.md)).

### 3b. Tooling Decisions

- **Prefer existing battle-tested packages** over custom scripts. Only write custom code when no viable alternative exists. Custom scripts introduce untested code that often fails in CI.
- **When migrating state** (databases, APIs, config), fetch current data from the live API rather than trusting local files or config. Local files may be stale.
- **When porting old code**, don't blindly copy the approach. Read the actual data format (JSON files, API responses, configs) and choose the robust technique. Old scripts often used quick hacks (regex on JSON, string splitting) that break on edge cases — use proper parsing in the new code.

### 4. Update Task Tracking

When tasks exist (via the agent's task tracking tools), mark each task `completed` **immediately after finishing it** — before moving to the next task. Never batch-update at the end. Never claim "all done" while the task list is stale.

### 5. E2E Tests for Frontend Changes

Any frontend change that affects UI behavior (new fields, form logic, visibility, navigation) requires **E2E tests as part of the implementation** — not as a follow-up. Include E2E test writing as an explicit task in the plan. If the project has a private test suite (`$T3_PRIVATE_TESTS`), write tests there. Post screenshots and a test plan to the MR before declaring complete.

- **When required:** new UI fields, changed form behavior, conditional visibility, new pages/routes
- **When NOT required:** pure CSS, translation-only changes, backend-only changes, internal refactoring
- **Backend/API changes with frontend-visible impact still require E2E.** If the frontend displays the changed data, prove the end-to-end path works.
- **Establish a baseline before blaming your branch.** If E2E fails, run the same scenario on the default branch or unmodified code before treating it as your regression.
- **Blocked by environment?** Flag it explicitly — don't silently skip E2E and declare done

### 5b. When to Switch to `t3-test`

- Stay in `t3-code` for TDD, implementation-time tests, and feature-building.
- Switch to `/t3-test` when the work becomes broader verification, E2E orchestration, CI failure analysis, test-plan writing, or MR evidence posting.

### 6. Quality Gates During Development

- Run linting after each significant change.
- Run type checking if the project uses it.
- Run the relevant test suite frequently — don't batch test runs.
- **Run the language convention skill's review checklist** (if loaded) before declaring implementation complete.
- **100% test coverage is part of the implementation (Non-Negotiable).** New code ships with tests in the same commit. Never lower coverage thresholds, add files to coverage omit lists, or exclude code from coverage measurement without **explicit user approval**. If you can't reach 100% coverage, the implementation scope is too large — break it into smaller pieces.

## Agent Rules

### Delegating Code to Sub-Agents

When launching parallel agents to write code (especially tests), include these requirements in every prompt:

- **Run `uv run ruff check <files>` and fix all violations** before declaring done
- **Run `uv run ruff format <files>`** to ensure formatting matches the project
- **Check type annotations** — if the project uses a type checker (ty, mypy), the code must pass
- **Never add `# noqa` without justification** — prefer fixing the issue

Sub-agents don't inherit project context. Include the specific linting rules and pre-commit expectations in their prompts.

### Systematic Debugging Protocol

If implementation breaks, switch to `t3-debug` and follow `systematic-debugging` before attempting speculative fixes.

### Fixing Existing Tests

When fixing a test you didn't write, apply **minimum viable changes**:

- **Only change the broken assertion** — don't restructure the test class, test framework, or data setup.
- **Never remove existing comments** — they document the original author's intent.
- **Never change test infrastructure** (e.g., `pytest.mark.django_db` → `TestCase`) unless explicitly asked.
- **Never add cleanup/delete calls** for data from migrations — make assertions robust instead (e.g., `sorted()` + subset `<=` checks).
- If the fix requires more than changing the assertion, ask the user first.

### Receiving Review Feedback

See `t3-review/SKILL.md § Receiving Code Review` for the full policy.

### Post-Implementation Retrospective

After completing work, run `/t3-retro` to review the session, capture lessons learned, and improve skills/playbooks.
