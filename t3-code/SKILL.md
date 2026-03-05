---
name: t3-code
description: Writing code with TDD methodology. Use when user says "implement", "write", "add", "code", "feature", "build", or is actively coding a solution.
compatibility: macOS/Linux, any language/framework supported by the project.
requires:
  - t3-workspace
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Writing Code (TDD)

## References

- [obra/superpowers](https://github.com/obra/superpowers) — TDD and development workflow skills

The implementation phase. Follow test-driven development and project conventions.

## Dependencies

- **t3-workspace** (required) — provides dev servers for live reload. **Load `/t3-workspace` now** if not already loaded.
- **Framework/language convention skills** (when backend is in scope) — e.g., Django conventions, Python style guides. Loaded automatically by the project overlay's companion skill config.

## Workflow

### 1. Plan First (Non-Negotiable)

**Always make a plan before writing code.** Never jump straight to coding.

- Identify scope: which files, modules, and repos are affected.
- Review existing patterns in the codebase before writing new code.
- If the task matches a playbook, follow playbook-specific patterns.
- **Feature flag check:** Follow the decision gate in [`references/multi-tenant-development.md`](../references/multi-tenant-development.md). Detect the target tenant using the priority chain from `t3-ticket § 6. Detect Variant/Tenant`.

**Scaling the plan to the task:**

- **Simple/clear tasks:** State the plan in a short bullet list, then start implementing immediately. No need for a dedicated plan mode or user confirmation.
- **Complex/ambiguous tasks:** Use the agent's planning workflow if it has one, explore the codebase, present the plan, and wait for approval before writing code.

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
- Feature flag rules for new features (see [`references/multi-tenant-development.md`](../references/multi-tenant-development.md)).

### 4. Update Task Tracking (Non-Negotiable)

When tasks exist (via the agent's task tracking tools), mark each task `completed` **immediately after finishing it** — before moving to the next task. Never batch-update at the end. Never claim "all done" while the task list is stale.

### 5. E2E Tests for Frontend Changes (Non-Negotiable)

Any frontend change that affects UI behavior (new fields, form logic, visibility, navigation) requires **E2E tests as part of the implementation** — not as a follow-up. Include E2E test writing as an explicit task in the plan. If the project has a private test suite (`$T3_PRIVATE_TESTS`), write tests there. Post screenshots and a test plan to the MR before declaring complete.

- **When required:** new UI fields, changed form behavior, conditional visibility, new pages/routes
- **When NOT required:** pure CSS, translation-only changes, backend-only changes, internal refactoring
- **Blocked by environment?** Flag it explicitly — don't silently skip E2E and declare done

### 6. Quality Gates During Development

- Run linting after each significant change.
- Run type checking if the project uses it.
- Run the relevant test suite frequently — don't batch test runs.
- **Run the language convention skill's review checklist** (if loaded) before declaring implementation complete.

## Agent Rules

### Collaboration Model

- **Ask, don't trial-and-error.** When a step fails, try ONE reasonable fix. If that doesn't work, **ask the user**.
- **Fix root causes in the skill.** Every failure that required trial-and-error → skill update.

### Systematic Debugging Protocol

See `t3-debug/SKILL.md` for the full 5-phase protocol. Key rule: **after 3+ failed fixes → STOP and ask the user.**

### Verification Before Claims

**Iron law:** No completion claims without fresh verification evidence in this message. See `t3-test/SKILL.md § Verification Before Claims` for the full evidence table.

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
