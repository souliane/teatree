# Architecture pre-check — TODO-130 (ruff anti-slop caps + jscpd duplication, layer 4)

## 1. BLUEPRINT § alignment

§17.6 (quality gates / fitness functions). This is layer 4 of the general
anti-drift design (wf_3c20a034 § "4. RUFF CAPS"): deterministic AST/structural
ceilings that make slop merge-blocking. No new BLUEPRINT section needed — extends
the existing quality-gate machinery (ruff caps + a new no-silent-skip hook +
jscpd duplication). BLUEPRINT mentions the gate family; the per-cap table lives
in the PR body, not the BLUEPRINT (per the size-budget gate).

## 2. FSM phase boundaries

n/a — no Ticket.State / Worktree.State transition. Pure tooling/config.

## 3. Extension-point contracts

n/a — no OverlayBase / scanner / hook-surface / *Backend Protocol change. New
pre-commit hook is a leaf (`scripts/hooks/check_no_silent_skip.py`); does not
participate in the overlay contract.

## 4. Component boundaries

- ruff caps → `pyproject.toml [tool.ruff]` (explicit mccabe pin; C901/FIX/ERA
  already active via `select=["ALL"]`).
- no-silent-skip guard → `scripts/hooks/check_no_silent_skip.py` (AST scanner,
  sibling of `check_module_health.py`).
- jscpd → `.pre-commit-config.yaml` local hook (language: node) + a config file
  `.jscpd.json`, mirroring the tach/import-linter local-hook wiring.
- conformance tests → `tests/quality/` (sibling of `test_chokepoints.py`).
No business logic touched; no straddling.

## 5. Dependency direction

No `src/teatree/` imports added. The new hook lives under `scripts/hooks/`
(exempt from tach module graph). `uv run tach check` unaffected.

## 6. Test surface

- `tests/quality/test_ruff_antislop_caps.py` — asserts C901/FIX/ERA stay
  enabled (a probe file with a too-complex fn / a TODO comment / commented-out
  code goes red under the project ruff config), and asserts the explicit
  mccabe pin is present and not loosened past the current value. RED if a
  future PR adds these to `lint.ignore` or raises the threshold.
- `tests/test_no_silent_skip_hook.py` — must-FLAG (`@pytest.mark.skip`,
  `skipif(True)`) + must-NOT-FLAG (conditional `skipif(shutil.which(...))`).
  RED-first: revert the guard, the must-flag cases stop blocking.
- `tests/quality/test_jscpd_duplication.py` — scan-COVERAGE assertion: every
  `src/teatree/**/*.py` file is in jscpd's analyzed set (no source escapes the
  scanner), and the config pins `--min-lines 50 --min-tokens 300 --threshold 0`.

## 7. Resilience invariants

No external write. The hooks are read-only AST/duplication scanners that exit
0/1. Idempotent (same tree → same verdict). No fallback transport / heartbeat /
sub-agent contract needed. jscpd via npx is provisioned by prek (`language:
node`), same as markdownlint-cli2's node provisioning.

## 8. Identity and key normalization

n/a — no bare-vs-qualified identity. File paths compared as-is (POSIX paths from
git / jscpd report). No strip/split-to-match.

## 9. Behavior preservation / capability deletion

Purely additive. NOT removing or weakening any matcher:

- subprocess.run/Popen/os.system ban stays in the chokepoint registry
  (`chokepoints.yaml::subprocess-egress`) — NOT moved to TID251 (would be a
  redundant second copy; the registry AST visitor is the stronger, single
  source of truth). The design said "move … if not already there" — it IS there.
- the 500-LOC cap stays in `check_module_health.py` (untouched).
- `check_quality_gates.py` stays the no-widening backstop (untouched); the
  explicit mccabe pin is written so it cannot be silently raised.
- string/diff bans (`--no-verify`, `core.hooksPath=`) NOT routed through ruff.
