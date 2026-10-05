# Live agent messaging and ACP compatibility decision

## Decision

TeaTree coordinates only sessions it launches. ACP is a client-to-agent control
protocol, not a peer-to-peer transport. Claude and Codex agents communicate
through TeaTree's MCP tools, backed by a private Unix socket. Messages exist only
while sender and recipient tasks run in the same worker process and ticket.
There is no durable mailbox, cross-worker routing, or idle-chat wake-up.

## Ownership and security

- The task runner assigns a random address and token to each live task. Neither
  an agent nor a generic MCP client can create a participant or select a room.
- The room is the TeaTree ticket ID. The token goes only to that task's TeaTree
  stdio MCP child. The broker socket lives in a mode-0700 temporary directory;
  the socket is mode 0600. The broker validates the token on every operation.
- `self`, `peers`, `send`, `inbox`, and bounded `wait` are the entire tool set.
  `send` is targeted, room-checked, byte-limited, and idempotent by sender key.
  Inbox memory is bounded and reports truncation if a cursor falls behind;
  response pages are byte-bounded after JSON encoding.
- Runner exit revokes the participant and discards its inbox. Process exit
  destroys the socket and all messages. The control database is not involved.
- The broker is a worker-local thread because each TeaTree task owns a separate
  `asyncio.run` loop and its MCP server is another process. A plain asyncio map
  in the runner loop would not be reachable from that child.

## ACP compatibility decision

Keep the existing `claude_sdk` and `codex_app_server` backends. ACP is a useful
standard client-to-agent control protocol, but it does not provide a peer-to-peer
channel and therefore does not replace the MCP mailbox. Stable ACP also lacks a
portable per-session developer-instruction field. The published `codex-acp`
adapter presently ignores the per-session system prompt metadata that Claude's
adapter understands (upstream issue #215). TeaTree's factory relies on those
instructions for role and phase policy, so an ACP cutover now would silently
weaken its policy. Do not ship an unused ACP dependency or selectable ACP harness
that cannot preserve this contract.

Future ACP admission requires an end-to-end test proving developer instructions,
tool/approval policy, private plan entitlement, session resume, usage, and
failure mapping for each adapter. An adapter that lacks one of these must be
unavailable for the affected phase, not silently fall back to a weaker policy.

The current Codex App Server harness must share one worker-owned process and
credential writer across simultaneous threads. Each TeaTree task retains its
own Codex thread, per-thread MCP settings and mailbox identity, and session ID.
This permits same-worker Codex↔Codex messages without concurrent writes to the
private Codex auth cache. Per-session persistence is serialized, and an idle
server exits after a short grace period to release the credential lock.

## Explicit limits

Stable ACP does not standardize unsolicited agent-to-agent message injection.
Agents call MCP inbox/wait themselves; TeaTree does not use experimental adapter
steering to wake busy or idle sessions. Adapter-specific steering may be used only
after capability detection and end-to-end tests. A different worker process has
a different live broker: decentralized factory coordination is a separate scope.
