# Architecture pre-check — souliane/teatree#128 (generic chokepoint registry)

## 1. BLUEPRINT § alignment

Extends the §17.1-invariant-2 flywheel (enforcement-as-structure) and the
quality-catalog machinery (`antipatterns.yaml`/`regression_rules.yaml`): one
declarative registry {protected symbol -> sole allowed module} + one generic
AST checker for call-site authorization. Distinct from tach (import graph) and
semgrep (intra-body shapes).

## 2. FSM phase boundaries

n/a — no `Ticket.State`/`Worktree.State` transition touched.

## 3. Extension-point contracts

n/a — no `OverlayBase`/scanner/hook-router/`*Backend` Protocol surface changed.
The registry references existing symbols (`subprocess.*`, `post_routed`,
`react_routed`, `react`, `post_message`) but adds no new contract.

## 4. Component boundaries

- Registry data: `src/teatree/quality/chokepoints.yaml` (sibling of
  `antipatterns.yaml`/`regression_rules.yaml`).
- Loader: `src/teatree/quality/chokepoints.py` (mirrors `regression_catalog.py`
  — yaml + frozen dataclass + load-time validation, stdlib only).
- Generic checker: `scripts/hooks/check_chokepoints.py` (generalizes
  `check_subprocess_ban.py`'s AST visitor; registry-driven).
- Conformance test: `tests/quality/test_chokepoints.py` (mirrors
  `test_catalog.py`).

## 5. Dependency direction

`teatree.quality` is `layer=foundation`, `depends_on=["teatree.utils"]`. The
loader imports only stdlib + `yaml` — adds no tach edge. The reachability-ledger
assertions that touch `teatree.backends.slack_bot` live in the TEST file (tests
are not tach-constrained). `uv run tach check` stays green.

## 6. Test surface

`tests/quality/test_chokepoints.py`:

- schema invariants (ids unique/kebab, `match_kind` enum, non-empty
  `allowed_modules`/`protected_attrs`);
- reachability ledger (every `allowed_module` resolves to a real file; every
  `protected_attr` is a real attribute on its declaring class/module);
- green-on-tree (zero violations on real `src/teatree/` — the blocking gate);
- anti-vacuous (synthetic `subprocess.run` / `x.post_routed()` outside allowed
  -> rc 1; inside -> rc 0; `def post_routed` -> rc 0; annotation/except -> rc 0);
- loader validation (bad enum / dup id / non-kebab / empty allowed rejected);
- self-maintenance Tier-2 (subprocess entry's `protected_attrs` superset of the
  historical `{run,Popen,check_output,check_call,call}`).

## 7. Resilience invariants

n/a — pure static-analysis gate, no external write, no DB row, no sub-agent.

## 8. Identity and key normalization

The canonical key is the fully-qualified dotted module path
(`teatree.utils.run`). `module_path_for(rel_path)` canonicalizes a scanned file
UP to its dotted path; `allowed_modules` are stored as dotted paths and compared
by identity — no `split`/`strip`-to-match seam.

## 9. Behavior preservation / capability deletion

Deletes `check_subprocess_ban.py` (+ test + pre-commit block) and
`tests/teatree_core/test_on_behalf_egress_import_guard.py`. The import-guard had
TWO invariants, BOTH preserved as registry entries:

- invariant 1 (`react_routed`/`post_routed` only inside `on_behalf_egress`)
  -> entry `on-behalf-routed-egress` (method-kind, allowed=`teatree.core.on_behalf_egress`);
- invariant 2 (`react`/`post_message` only at documented bot->user/self-ack
  sinks, with the receiver-is-egress carve-out) -> entry
  `on-behalf-colleague-primitives` (method-kind + `exempt_receivers:
  [egress, OnBehalfSlackEgress]`, allowed = the documented sink modules).
`exempt_receivers` is an optional refinement of the existing `method` kind, NOT
a third `match_kind` — the DSL stays two-valued. No must-block test inverted to
must-not-block. `os.system` deliberately NOT added (zero hits; the deleted ban
excluded it — flagged in the commit body).
DEFER (tracked follow-ups, would break green): httpx/requests (~15 modules),
gh/glab forge argv (different matcher), secrets `read_pass` (~9 modules),
merge-keystone (`merge_ticket_pr`/`record_merge_and_advance` are bare-name
function calls — neither `module_attr` nor `method`; registering needs a third
match_kind = scope creep). KEEP SEPARATE (term/diff scans, not call sites):
`check_no_overlay_leak.py`, banned-terms, privacy-push-scan.
