# Architecture pre-check — souliane/teatree#1881

## 1. BLUEPRINT § alignment

Robustness fix in `teatree.backends.slack_reactions`. No BLUEPRINT section change — the
never-block-FSM contract (the reason `add_reaction` already degrades transport failures to
`return False`) is unchanged; this extends that same degradation to an unparsable 2xx body.

## 2. FSM phase boundaries

n/a — no `Ticket.State` / `Worktree.State` transition added or moved. The FSM-side wrappers
(`add_reactions_for_transition`, `add_approval_reaction`) keep their existing contract.

## 3. Extension-point contracts

n/a — no `OverlayBase` / scanner / hook / `*Backend` Protocol surface changed. `add_reaction`
keeps its signature `(token, channel_id, timestamp, emoji) -> bool` and its existing raise/return
contract.

## 4. Component boundaries

`src/teatree/backends/` — the Slack reaction transport. The guard stays inside the existing
`add_reaction` function; no new module.

## 5. Dependency direction

Imports added: `json` (stdlib). No backwards edge. `uv run tach check` confirmed.

## 6. Test surface

`tests/teatree_backends/test_slack_reactions.py::TestAddReaction` — a 2xx response whose `.json()`
raises `json.JSONDecodeError` asserts `add_reaction(...) is False` and does NOT propagate the
exception. Regression observed RED on pre-fix code (uncaught `.json()`).

## 7. Resilience invariants

Single external read (`response.json()` parse). fail-closed-on-ambiguity: an unparsable 2xx body
degrades to the existing failure contract (`return False`, logged) instead of crashing the caller —
the same shape transport failures (HTTP 5xx, `httpx.HTTPError`) already use. Idempotent: a failed
reaction returns `False`; the next FSM tick re-attempts. No write, no consume/audit, no sub-agent.

## 8. Identity and key normalization

n/a — no bare-vs-qualified identity in scope.

## 9. Behavior preservation / capability deletion

Purely additive guard. All existing branches preserved: `ok:true` → True, `already_reacted` → True,
other `ok:false` → raise `SlackReactionError`, HTTP non-2xx / `httpx.HTTPError` → False. The new
branch only covers the previously-uncaught case (2xx + unparsable body) and routes it to the
existing `return False` failure contract. No must-block test inverted.
