# TeaTree — Agent Instructions

## Code Quality Standard (applies to all reviews and implementations)

Every code change — implementation, refactor, or review fix — must meet this bar:

- **Full typing.** All functions and methods have modern type annotations (`list[str]`, `str | None`). No `Any` unless interfacing with untyped third-party code.
- **Composition over mixins.** Split large classes using composition (`OverlayConfig`, `OverlayMetadata` as attributes on `OverlayBase`). Avoid mixin inheritance — use composed classes instead. Module-level functions only when a class adds no value.
- **Self-documenting hierarchy.** File names, module names, and class names tell the reader what they contain. No comments needed for structure — the structure IS the documentation.
- **Django conventions.** Follow Django's own code style: managers for QuerySet logic, model methods for instance behavior, views for coordination only. Don't fight the framework — if Django has a pattern for it, use that pattern. Comment when intentionally diverging.
- **Tests mirror production code.** Test files mirror the `src/` module path. Test classes and methods describe behavior, not implementation. Integration tests for happy paths, unit tests for edge cases.
- **No tech debt without explicit approval.** Never suppress lint rules (`# noqa`, `per-file-ignores`), lower coverage thresholds, or introduce workarounds without asking. Fix the architecture instead. If fixing requires significant refactoring, present options and ask.
- **Documentation alignment.** Every code change must leave docs, skills, BLUEPRINT.md, and generated docs consistent with the code. Mermaid diagrams must reflect current architecture. Procedures must reference current CLI commands and APIs.
- **No stale references.** Renamed settings, removed CLI commands, changed entry point formats — all consumers must be updated in the same change. Grep across all repos in scope.

## Architecture (issue #61 — teatree as Django project)

Teatree IS the Django project. Overlays are lightweight Python packages:

- Overlays register via `teatree.overlays` entry points (value = overlay class path)
- Overlay-specific config lives on `OverlayBase` methods (not Django settings)
- `OverlayBase` uses composition: `overlay.config` is an `OverlayConfig` instance (credentials, URLs, labels)
- Multi-overlay support: `overlay` CharField on Ticket, Worktree, Session
- `pip install teatree` works — no repo checkout or `uv` needed
- CLI: `t3 startoverlay` (not `startproject`) creates lightweight overlay packages

## Running things

```bash
uv run pytest --no-cov -x -q   # run tests
uv run ruff check               # lint
uv run ruff format               # format
t3 --help                        # CLI (installed via `uv tool install --editable .`)
```
