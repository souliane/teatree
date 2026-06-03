# Architecture pre-check — OnBehalfSlackEgress (away-mode colleague-react incident)

## 1. BLUEPRINT § alignment

§ "Slack token routing" (#1750 route_token self-vs-colleague) + the on-behalf gate
section (`on_behalf_post_mode`, `require_on_behalf_approval`, `notify_user_on_behalf_post`).
Claim: every colleague-surface Slack post/react under the user's identity flows through
one cohesive `OnBehalfSlackEgress` that runs gate→route→emit→audit in one place; self-DM
short-circuits ungated via the same #1750 classifier (fail-closed on unknown surface).

## 2. FSM phase boundaries

n/a — no `Ticket.State` / `Worktree.State` transition added. The FSM-driven signals.py
reaction path is deliberately out of scope (separate slack_reactions transport, already
gate+audit-correct).

## 3. Extension-point contracts

No `OverlayBase` / scanner-registration / `*Backend` Protocol change. `MessagingBackend`
Protocol is unchanged — the self-DM classifier duck-types `route_token` (not on the
Protocol) so no Protocol surface grows and no fake needs updating. Consumers rewired:
review_claim.emit_review_done_reactions, review_request_merge_react.react_merge_on_post,
slack_broadcasts._react_done, review_nag._post_thread_nag, notify.Command.post/react,
slack_listen.react_command +_ack_messages.

## 4. Component boundaries

`src/teatree/core/on_behalf_egress.py`. Both reused seams (the gate
`on_behalf_gate_recorded`, the audit `on_behalf_post_receipt`) already live in
teatree.core; `MessagingBackend`/`RawAPIDict` are in teatree.backends/teatree.types which
core depends on. teatree.messaging is the wrong home (the gate+audit are not there).

## 5. Dependency direction

Imports: require_on_behalf_approval + OnBehalfPostBlockedError (teatree.core),
notify_user_on_behalf_post (teatree.core), MessagingBackend (teatree.backends), RawAPIDict
(teatree.types). All existing edges in tach.toml `teatree.core` depends_on. No new edge.
`uv run tach check` confirms.

## 6. Test surface

- bypass-closed (RED-now) for the 4 loop sites: under ask + no approval, the routed
  primitive is NOT called and the claim is released / a `.gated` signal emitted.
- satisfiable must-FIRE: with a recorded OnBehalfApproval, reacts/posts once + one
  `on_behalf_post:<target>:<action>` BotPing.
- self-DM carve-out: route_token classifies self → emit, no raise, no BotPing, no approval
  consumed; colleague D… blocks.
- fail-closed: backend with no route_token → BLOCKS under ask.
- audit-only-on-success: ok:false / already_reacted → no notify_user_on_behalf_post.
- CLI bypass closed (notify react colleague → SystemExit 2; self ungated); ad-hoc
  `t3 slack react` colleague blocks; `_ack_messages` self stays ungated.
- import-guard fitness function: no module except on_behalf_egress.py calls
  `.react_routed(`/`.post_routed(`; no colleague `.react(`/`.post_message(` outside the
  documented self-ack/bot→user sinks.

## 7. Resilience invariants

External write = Slack post/react. verify-by-re-read: callers keep inspecting the raw
Slack body (ok/error). idempotency: each call site keeps its pre-existing claim
(OutboundClaim ledger / done_at / last_nag_step); the audit DM is deduped per
(target, action) by notify_user_on_behalf_post's idempotency key. fallback-transport:
unchanged per site (claim release on block/transport error). heartbeat: n/a (single call).
sub-agent return contract: n/a.

## 8. Identity and key normalization

target is canonicalized UP to the on-behalf canonical form by OnBehalfApproval.consume /
require_on_behalf_approval — the egress passes target through verbatim; no strip/split to
make a comparison succeed.

## 9. Behavior preservation / capability deletion

Replaces scattered raw egress with the class. Preserved per site: #1838 self-author skip
(before the gate), done_at / last_nag_step atomic claim (before the gate),
ConnectChannelBotRestrictedError re-raise + dedup (slack_broadcasts), the ok/error/
already_reacted/missing_scope mapping (returned raw body). Dropped: review_claim._react_routed
helper, slack_listen.post_reaction +_resolve_reaction_token (raw personal-xoxp
reactions.add path) — clean cutover, no shim. No privacy/security matcher narrowed. No
must-block test inverted.
