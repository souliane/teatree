# TeaTree — Agent Instructions

## Code Quality Standard (applies to all reviews and implementations)

Every code change — implementation, refactor, or review fix — must meet this bar:

- **Full typing.** All functions and methods have modern type annotations (`list[str]`, `str | None`). No `Any` unless interfacing with untyped third-party code.
- **Composition over mixins.** Split large classes using composition (`OverlayConfig`, `OverlayMetadata` as attributes on `OverlayBase`). Avoid mixin inheritance — use composed classes instead. Module-level functions only when a class adds no value.
- **Self-documenting hierarchy.** File names, module names, and class names tell the reader what they contain. No comments needed for structure — the structure IS the documentation.
- **Django conventions.** Follow Django's own code style: managers for QuerySet logic, model methods for instance behavior, views for coordination only. Don't fight the framework — if Django has a pattern for it, use that pattern. Comment when intentionally diverging.
- **Tests mirror production code.** Test files mirror the `src/` module path. Test classes and methods describe behavior, not implementation. **New tests lean integration / E2E / functional** — Django test client, `call_command`, real `git` under `tmp_path`. Unit tests are reserved for pure logic (parsers, formatters, branch-name builders). Mock only unstoppable externals (network, clock, third-party subprocesses). See `AGENTS.md` § "Test-Writing Doctrine" for the full rule and review gate.
- **No tech debt without explicit approval.** Never suppress lint rules (`# noqa`, `per-file-ignores`), lower coverage thresholds, or introduce workarounds without asking. Fix the architecture instead. If fixing requires significant refactoring, present options and ask.
- **Documentation alignment.** Every code change must leave docs, skills, BLUEPRINT.md, and generated docs consistent with the code. Mermaid diagrams must reflect current architecture. Procedures must reference current CLI commands and APIs.
- **No stale references.** Renamed settings, removed CLI commands, changed entry point formats — all consumers must be updated in the same change. Grep across all repos in scope.

## Architecture (issue #61 — teatree as Django project)

Teatree IS the Django project. Overlays are lightweight Python packages:

- Overlays register via `teatree.overlays` entry points (value = overlay class path)
- Overlay-specific config lives on `OverlayBase` methods (not Django settings)
- `OverlayBase` uses composition: `overlay.config` is an `OverlayConfig` instance (credentials, URLs, labels)
- Multi-overlay support: `overlay` CharField on Ticket, Worktree, Session
- Installed from a local clone via `uv tool install --editable .` then `t3 setup`
- CLI: `t3 startoverlay` (not `startproject`) creates lightweight overlay packages

## Running things

```bash
uv run pytest                    # full suite, parallel (-n auto), no coverage — fast default
bash dev/test-cov.sh             # coverage lane: --cov --doctest-modules, 93% floor (CI parity)
bash dev/ci-parity-fast.sh       # inner-loop: scoped prek + makemigrations + push gate, NO floor
bash dev/ci-parity.sh            # the EXACT full CI predicate in one command (see below)
uv run ruff check                # lint
uv run ruff format               # format
t3 tool push-gate                # inspect the incremental push-gate plan for the current diff (#122)
t3 --help                        # CLI (installed via `uv tool install --editable .`)
```

**Before pushing a src-touching PR, run `bash dev/ci-parity.sh`.** It chains the
exact blocking CI predicate (prek all-files, `makemigrations --check`,
`t3 tool test-path-mirror`, `dev/test-cov.sh`, `t3 ci coverage`) so a floor/gate
failure is caught locally instead of on the first CI cycle. It is opt-in by
workflow, never a push hook — the 93% whole-tree coverage floor is a whole-tree
property no diff-scoped push subset can prove, and the full suite must never gate
a push (`tests/test_no_full_suite_on_pre_push.py`). Use `bash dev/ci-parity-fast.sh`
for the fast inner loop while iterating. The push-stage `ci-critical-parity` hook
runs `dev/push-gate.sh` — the never-lockout safety contract plus the incremental
push gate (scoped doctest + ast-grep, FULL on any uncertainty, behind the
default-TRUE `incremental_push_gate` flag — ON scopes the diff, OFF is the pre-#122
whole-tree run; the CI whole-tree backstop is untouched). The broad `tests/quality`
dir is CI-only (it ran ~420s locally — the `test (3.13)` shard covers it whole-tree).
