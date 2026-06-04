# Architecture pre-check

## souliane/teatree#1880 — CAS-stamp the loop receipt only after the side-effect lands

### 1. BLUEPRINT § alignment

§17.1 invariant 2 (resilience: verify-by-re-read / idempotency / retry-safety on external writes)
and §5.6 (loop topology — the reactive Slack-answer cycle). Claim: a loop side-effect (Slack
react/reply) must land before its receipt is finalized, so a failed side-effect rolls the claim
back and retries, never leaves a lying receipt.

### 2. FSM phase boundaries

n/a — no `Ticket.State` / `Worktree.State` transition. The change is on the per-row CAS receipt
columns of `PendingChatInjection` (`eyes_reacted_at`, `loop_replied_at`), not the session FSM.

### 3. Extension-point contracts

No `OverlayBase` / scanner-registration / `*Backend` Protocol surface changes. `MessagingBackend.react`
is consumed unchanged. The fix is internal to `cycle.py` + two new rollback CAS methods on the
`PendingChatInjection` model.

### 4. Component boundaries

- `src/teatree/core/models/pending_chat_injection.py` — the rollback CAS methods (`unmark_eyes_reacted`,
  `unmark_loop_replied`) live on the model (Fat Model: the single-use CAS lives with the data, beside
  `mark_eyes_reacted` / `mark_loop_replied`).
- `src/teatree/loop/slack_answer/cycle.py` — the claim→side-effect→release-on-failure sequencing
  (loop body orchestration).

### 5. Dependency direction

No new imports. `cycle.py` already imports the model; the model gains no imports. No backwards edge.

### 6. Test surface

`tests/teatree_loop/slack_answer/test_cycle_internals.py`:

- eyes: `backend.react` raises → `eyes_reacted_at` is rolled back to NULL (row retries) — RED on pre-fix.
- eyes: success path stamps `eyes_reacted_at` exactly once.
- eyes: two concurrent attempts react exactly once (CAS exclusivity preserved).
- ack: `backend.react` raises → `loop_replied_at`/`answer_kind` rolled back for the whole unit.
- ack: success path stamps once.
`tests/teatree_core/test_pending_chat_injection_model.py`: the rollback CAS methods round-trip.

### 7. Resilience invariants

- verify-by-re-read: success path keeps the existing `verify_reply_visible` readback (SIMPLE) and now
  the eyes/ack receipt is only durable after the side-effect returns without raising.
- fallback-transport: n/a (the durable retry IS the fallback — the row stays in `loop_unreplied()`).
- idempotency: the CAS claim is the idempotency lock — a re-run / concurrent tick matches 0 rows.
  Rollback only fires on the claimant's own failure, so it cannot release another tick's claim.
- heartbeat: n/a (bounded per-cycle batch).
- sub-agent return contract: n/a.

### 8. Identity and key normalization

n/a — no bare-vs-qualified identity. Rows are keyed by pk in the conditional UPDATEs.

### 9. Behavior preservation / capability deletion

Purely additive sequencing change. Exactly-once (the CAS claim) is preserved unchanged; the only new
behavior is rollback-on-failure. No matcher narrowed, no must-block test inverted. `_handle_simple`
already had the correct post→verify→stamp order and is untouched. The WHEN semantics
(react-eyes-once, ack-only-when-classified-ACK) and dedup are unchanged.

## TODO-130 — ruff anti-slop caps + jscpd duplication (layer 4)

### 1. BLUEPRINT § alignment

§17.6 (quality gates / fitness functions). This is layer 4 of the general
anti-drift design (wf_3c20a034 § "4. RUFF CAPS"): deterministic AST/structural
ceilings that make slop merge-blocking. No new BLUEPRINT section needed — extends
the existing quality-gate machinery (ruff caps + a new no-silent-skip hook +
jscpd duplication). BLUEPRINT mentions the gate family; the per-cap table lives
in the PR body, not the BLUEPRINT (per the size-budget gate).

### 2. FSM phase boundaries

n/a — no Ticket.State / Worktree.State transition. Pure tooling/config.

### 3. Extension-point contracts

n/a — no OverlayBase / scanner / hook-surface / *Backend Protocol change. New
pre-commit hook is a leaf (`scripts/hooks/check_no_silent_skip.py`); does not
participate in the overlay contract.

### 4. Component boundaries

- ruff caps → `pyproject.toml [tool.ruff]` (explicit mccabe pin; C901/FIX/ERA
  already active via `select=["ALL"]`).
- no-silent-skip guard → `scripts/hooks/check_no_silent_skip.py` (AST scanner,
  sibling of `check_module_health.py`).
- jscpd → `.pre-commit-config.yaml` local hook (language: node) + a config file
  `.jscpd.json`, mirroring the tach/import-linter local-hook wiring.
- conformance tests → `tests/quality/` (sibling of `test_chokepoints.py`).
No business logic touched; no straddling.

### 5. Dependency direction

No `src/teatree/` imports added. The new hook lives under `scripts/hooks/`
(exempt from tach module graph). `uv run tach check` unaffected.

### 6. Test surface

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

### 7. Resilience invariants

No external write. The hooks are read-only AST/duplication scanners that exit
0/1. Idempotent (same tree → same verdict). No fallback transport / heartbeat /
sub-agent contract needed. jscpd via npx is provisioned by prek (`language:
node`), same as markdownlint-cli2's node provisioning.

### 8. Identity and key normalization

n/a — no bare-vs-qualified identity. File paths compared as-is (POSIX paths from
git / jscpd report). No strip/split-to-match.

### 9. Behavior preservation / capability deletion

Purely additive. NOT removing or weakening any matcher:

- subprocess.run/Popen/os.system ban stays in the chokepoint registry
  (`chokepoints.yaml::subprocess-egress`) — NOT moved to TID251 (would be a
  redundant second copy; the registry AST visitor is the stronger, single
  source of truth). The design said "move … if not already there" — it IS there.
- the 500-LOC cap stays in `check_module_health.py` (untouched).
- `check_quality_gates.py` stays the no-widening backstop (untouched); the
  explicit mccabe pin is written so it cannot be silently raised.
- string/diff bans (`--no-verify`, `core.hooksPath=`) NOT routed through ruff.
