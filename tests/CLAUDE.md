# tests — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `tests/`.

- **Run:** `uv run pytest` for a fast inner loop — the default is parallel (`-n auto`) with no coverage. Add `-x -q` to stop on first failure. The full coverage gate (`bash dev/test-cov.sh`, and the CI `test (3.13)` lane) is **93% branch, non-negotiable** (`fail_under = 93, branch = true`; migrations omitted). New code ships with its tests in the same commit.
- **Before pushing a src-touching PR, run `bash dev/ci-parity.sh`** — the exact full CI predicate (prek, `makemigrations --check`, `t3 tool test-path-mirror`, `dev/test-cov.sh`, `t3 ci coverage`) in one command. The push-stage `ci-critical-parity` hook only runs the fast scoped lane (`tests/quality` + the never-lockout contract + `--doctest-modules src/teatree`); the whole-tree coverage floor is provable only by the full lane, so it stays opt-in, never a push hook (`tests/test_no_full_suite_on_pre_push.py`).
- **Tests mirror `src/`.** Test path mirrors the module under test; classes/methods describe behaviour, not implementation.
- **Lean integration / functional.** Prefer the Django test client, `call_command`, and real `git` under `tmp_path`. Reserve unit tests for pure logic (parsers, formatters, branch-name builders). Mock only unstoppable externals (network, clock, third-party subprocesses). Full rule + review gate: `AGENTS.md` § "Test-Writing Doctrine".
- **A regression test must be observed RED before the fix.** A test that passes on the buggy code guards nothing — see `/t3:code` § TDD Discipline.
