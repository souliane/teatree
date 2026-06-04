# Architecture pre-check — souliane/teatree#1879

Make on-behalf consume+audit+post atomic so a failed post cannot burn the
approval or write a lying audit.

## 1. BLUEPRINT § alignment

§17.4 (recorded-approval / clear+audit family) and `/t3:rules` § "Ask Before
Posting on the User's Behalf". The on-behalf gate consumes a single-use
`OnBehalfApproval`, writes an `OnBehalfAudit`, and the caller posts — these
three must be all-or-nothing. No BLUEPRINT prose change: the documented
outcome table is unchanged; only the consume+post+audit ordering becomes
atomic.

## 2. FSM phase boundaries

n/a — no `Ticket.State` / `Worktree.State` transition. The PR-approval and
ticket-transition signal receivers call the gate, but the FSM transition
itself is unchanged and never blocked.

## 3. Extension-point contracts

No `OverlayBase` / scanner-registration / `*Backend` Protocol change. The
chokepoint registry entry #1 (`on-behalf-routed-egress`: post_routed /
react_routed) protects backend symbols, not `require_on_behalf_approval`'s
signature — unaffected; `tests/quality/test_chokepoints.py` stays green (37
passing). Consumers of the gate, updated in lockstep to the callback form:

- `teatree.core.on_behalf_egress` (post / react)
- `teatree.core.reply_transport` (_send / redeliver)
- `teatree.core.signals` (transition / approval reaction)
- `teatree.core.management.commands.review_request_post`
- `teatree.core.management.commands.pr` (post_evidence)
- `teatree.core.management.commands._e2e_evidence`
- `teatree.cli.review_on_behalf` (peek `check_on_behalf` +
  consuming `publish_on_behalf`) and `teatree.cli.review.ReviewService`
  (8 post sites wrapped in `_publish_or_blocked`).

## 4. Component boundaries

`teatree.core.on_behalf_gate_recorded` — unchanged home. Split into two
purpose-typed functions: `require_on_behalf_approval(publish=…)` (the only
consuming path: consume + callback + audit in one `transaction.atomic`) and
`on_behalf_block_message(target, action)` (non-consuming peek for early
refusal). `OnBehalfApproval.has_unconsumed` backs the peek.

## 5. Dependency direction

No new cross-module imports — the publish callback is passed in by each
caller. `uv run tach check` → "All modules validated!".

## 6. Test surface

- `tests/teatree_core/test_on_behalf_gate_recorded.py`
  `TestPublishCallbackAtomicity`: failed publish rolls back consume + writes
  no audit (RED-observed on pre-fix non-atomic order); success runs
  publish→consume→audit; BLOCK+no-approval never runs publish; retry reuses
  the approval. `TestNonConsumingPeek`: peek never consumes / DMs.
- `tests/teatree_core/test_reply_transport_on_behalf_gate.py`
  `TestRedeliverReusesReservation`: a failed redeliver does not burn the
  approval; N redelivers consume exactly one (RED-observed on pre-fix
  redeliver shape).
- `tests/teatree_core/test_review_request_post_command.py`: no-backend
  suppress no longer consumes (improved behavior).

## 7. Resilience invariants

External write = the colleague post (callback).

- verify-by-re-read / idempotency: `OnBehalfApproval.consume` keeps its
  `select_for_update` + `consumed_at` single-use claim, now inside the same
  atomic block as the post; `reply_transport` keeps its ReplyDispatch
  reservation.
- fallback-transport: BLOCK + no approval still raises
  `OnBehalfPostBlockedError`; the caller surfaces the blocked post.
- NEW invariant **atomicity**: consume + post + audit share one
  `transaction.atomic` — a post failure rolls back the consume (no burn) and
  writes no audit (no lie). Enforced structurally by flipping the
  `consume-before-side-effect-not-atomic` semgrep rule warn→blocking in the
  same PR.
- heartbeat / sub-agent return contract: n/a (synchronous single post).

## 8. Identity and key normalization

`canonical_on_behalf_target` already canonicalizes the target up to
`<repo>!<iid>` at both record and consume; `has_unconsumed` reuses it. No new
identity surface; no strip/split-to-match introduced.

## 9. Behavior preservation / capability deletion

Rewrites `require_on_behalf_approval`'s body to take a publish callback.
Behaviors preserved: PROCEED (run post, no consume/audit); AUTO_DRAFT (DM +
run post, no consume/audit); BLOCK+approval (consume+post+audit, now atomic);
BLOCK+no-approval (raise, never post). `check_on_behalf` keeps its
no-publish early-refusal form but is now non-consuming (the consume moved to
the post site). No privacy/leak/security matcher narrowed; no must-block test
inverted. The semgrep warn rule is flipped to blocking in the same PR (proven
to bite the pre-fix code and green-on-tree after the fix).
