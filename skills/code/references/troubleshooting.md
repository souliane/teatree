# Troubleshooting

> Load when diagnosing CLI, management command, or overlay wiring failures.

---

## Overlay Groups Show Only a Generic `run` Passthrough

- **Symptom:** `t3 <overlay> <group> --help` shows a single `run` subcommand instead of the actual management command subcommands (e.g., `refresh`, `restore-ci`, `reset-passwords`).
- **Cause:** `_build_overlay_app` bridges management commands via `_bridge_subcommand` — if the `DJANGO_GROUPS` registry doesn't list individual subcommands, only the generic passthrough appears.
- **Fix:** Add each subcommand explicitly to `DJANGO_GROUPS` in `cli/overlay.py` with `(name, help_text)` tuples. The bridge creates one Typer command per entry that delegates to `manage.py <group> <subcommand>`.
- **Prevention:** When adding, renaming, or deleting a management command or subcommand, always update the matching entry in `DJANGO_GROUPS` in the **same commit**. The bridge does not auto-discover Django commands — a missing entry is silent (no group at all), and a stale entry dispatches to a non-existent backend (each call 404s with `Unknown command`).

## Overlay Bridge Dispatches to a Deleted Django Command

- **Symptom:** `t3 <overlay> <group> <sub>` exits with `Unknown command: '<group>'` after a recent rename/refactor of the underlying management command, even though `t3 <overlay> <group> --help` lists the subcommand.
- **Cause:** `cli/overlay.py:DJANGO_GROUPS` still maps `<group>` to a Django command that has been renamed or deleted. The bridge happily forwards to a target that no longer exists; the failure surfaces only when the user runs the subcommand for real.
- **Fix:** Update `DJANGO_GROUPS` to point at the current Django command names (e.g. when `lifecycle` was split into `worktree` + `workspace`, the bridge needed both groups, not the old one). Also retire any docs/skills that still mention the dead group.
- **Prevention:** The existing `tests/test_cli_overlay.py` bridge tests mock `managepy`, so they pass even when the target is dead. Treat any rename/deletion of a Django command in `core/management/commands/` as a `DJANGO_GROUPS` change too — and run `t3 <overlay> <group> <sub> --help` end-to-end once after the change to confirm the dispatch resolves.

## Doc-Only "Fix" Propagates a Broken CLI Reference

- **Symptom:** A docs-only commit replaces stale CLI invocations (e.g. `t3 lifecycle setup` → `t3 <overlay> lifecycle setup`) but every reader who copy-pastes the new form still gets `Unknown command`.
- **Cause:** The retarget assumed the new command exists because the old one used to. When the underlying Django command (or bridge entry) was deleted in a prior PR, prose retargeting alone can't make the new invocation work — it just relocates the broken reference.
- **Fix:** Before shipping a doc-only retarget of a CLI invocation, smoke-test the new form with `t3 <overlay> <group> <sub> --help`. If the call fails, the docs aren't stale — the CLI is broken, and the right scope is "fix the CLI bridge **and** the docs in one PR".
- **Prevention:** Treat "stale `t3` reference" findings as a tripwire, not a docs typo. Always check `cli/overlay.py:DJANGO_GROUPS` and `core/management/commands/` before shipping the retarget.

## CLI References Undefined Sub-Apps After Script Migration

- **Symptom:** `NameError: name 'workspace_app' is not defined` when running any `t3` command.
- **Cause:** During migration from scripts to management commands, old references to top-level Typer sub-apps (e.g., `workspace_app`, `pr_app`) were left in `_build_overlay_app` but the apps themselves were removed.
- **Fix:** Remove dead references. The `_DJANGO_GROUPS` loop now handles all overlay command groups.
- **Prevention:** After deleting code, grep for references to deleted symbols before committing.

## Overlay Discovery Returns Empty Despite Config

- **Symptom:** `t3 info` shows no installed overlays even though `~/.teatree.toml` has `[overlays.*]` sections.
- **Cause:** `discover_overlays()` was only reading entry points, not the toml config.
- **Fix:** `discover_overlays()` now reads `[overlays.<name>]` sections from `~/.teatree.toml` first, then falls back to entry points. Toml entries win on name conflict.
- **Prevention:** When adding new discovery sources, test with both the toml config and entry points.

## prek Discovers Template Directory as Sub-Project

- **Symptom:** `uv run prek run pytest` collects 0 tests and exits with code 5, blocking all commits. Output shows `Running hooks for src/teatree/templates/overlay:`.
- **Cause:** prek auto-discovers sub-projects by scanning for `.pre-commit-config.yaml`. The scaffold template directory had a real `.pre-commit-config.yaml` that prek treated as a project root. The `always_run: true` pytest hook then ran in the template scope (0 tests → fail), and `fail_fast: true` prevented the root scope from ever running.
- **Fix:** Rename `src/teatree/templates/overlay/.pre-commit-config.yaml` to `.pre-commit-config.yaml.tmpl`. Update `_copy_config_templates()` in `cli.py` to map `.tmpl` → `.yaml` when scaffolding.
- **Prevention:** Never put real config files (`.pre-commit-config.yaml`, `pyproject.toml`) in template directories. Use `.tmpl` suffix for all template files that prek or other tools might auto-discover.

## Complexity Suppressions Are Not Fixes

- **Symptom:** Adding `# noqa: C901, PLR0912, PLR0914` to suppress complexity warnings.
- **Cause:** Function has too many branches/locals/complexity. Suppressing hides the problem.
- **Fix:** Extract helper functions. In the `build_active_sessions` case: `_classify_session_kind()`, `_extract_task_context()`, `_extract_launch_url()`. Replace magic numbers with named constants (`_MIN_PS_COLUMNS = 3`).
- **Prevention:** Never suppress C901/PLR0912/PLR0914. Always refactor. If the function genuinely can't be simplified, document why in a comment next to the noqa.

## Prefer Explicit Domain Methods Over Signals for Side Effects

- **Symptom:** Proposed django-fsm `post_transition` signals to auto-schedule tasks after state transitions. Would have required signals.py, app.ready() registration, transaction.on_commit, and loop prevention logic.
- **Cause:** Over-engineering. Signals add hidden indirection — you have to know signals.py exists to understand what `ticket.test()` does. Debugging is harder, testing needs TransactionTestCase.
- **Fix:** Put side effects directly in the transition method body: `self.schedule_review()` inside `test()`. For task completion advancing tickets, use `_advance_ticket()` called from `Task.complete()`.
- **Prevention:** Always ask "can I just call a method here?" before reaching for signals, hooks, or event systems. Explicit beats implicit for maintainability.
