# tests — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `tests/`.

- **Run:** `uv run pytest --no-cov -x -q` for a fast inner loop. The full coverage gate is **93% branch, non-negotiable** (`fail_under = 93, branch = true`; migrations omitted). New code ships with its tests in the same commit.
- **Tests mirror `src/`.** Test path mirrors the module under test; classes/methods describe behaviour, not implementation.
- **Lean integration / functional.** Prefer the Django test client, `call_command`, and real `git` under `tmp_path`. Reserve unit tests for pure logic (parsers, formatters, branch-name builders). Mock only unstoppable externals (network, clock, third-party subprocesses). Full rule + review gate: `AGENTS.md` § "Test-Writing Doctrine".
- **A regression test must be observed RED before the fix.** A test that passes on the buggy code guards nothing — see `/t3:code` § TDD Discipline.
