# Live mailbox and ACP compatibility: implementation plan

One delivery phase, one PR per repository. Preserve setup's private agent homes,
existing harness policy, resume lineage, and model routing. Work test-first.

1. Replace the DB mailbox draft with a worker-local Unix-socket broker. RED:
   two Codex participants exchange; Claude↔Codex room isolation; forged identity,
   duplicate send, escaped-body and encoded-page limits, bounded wait,
   unregister, and two actual stdio MCP children. GREEN: broker and thin MCP
   client. Remove the draft model, migration, and reconnect semantics.
2. Bind identities in the real runner immediately before `harness.open`, pass
   the token only in TeaTree MCP server environment, revoke in `finally`.
   RED/GREEN: runner binding, conditional MCP registration, child access,
   reader phase absence, and task-end revocation.
3. Make the production Codex App Server worker-owned instead of task-owned, so
   multiple Codex threads can be live concurrently while one auth-cache lock and
   process own the private CODEX_HOME. RED/GREEN with two fake-server sessions,
   private per-thread MCP configuration, prompt/stream/result, serialized auth
   persistence, idle lock release, failed-auth recovery, and resume lineage.
   Preserve existing single-session behavior for injected fake-command tests.
4. Record the ACP parity gate in the design. Do not install adapters or the ACP
   SDK until the published Codex adapter can forward TeaTree's per-task developer
   instructions and an end-to-end test proves the full policy and plan-auth
   contract. ACP does not replace the live mailbox.
5. Update README/BLUEPRINT with exact live-only semantics. Run targeted tests,
   affected harness and MCP suites, format/lint, type checks, and the repository's
   quality gate. Cold-review the exact diff before requesting review or merge.
