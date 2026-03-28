# Agent Backend Decision

**Decision:** TeaTree uses Claude as the sole agent backend (see BLUEPRINT.md §5.7).

## Implications for Tests

- **No Codex integration tests needed.** The generic agent abstraction was removed.
- **`EchoRuntime` is for tests only.** It provides a deterministic, no-network
  runtime for testing the dispatch and session management code paths.
- **`Session.agent_id` is a Claude session ID** used for resume functionality,
  not an agent backend identifier.

If a second agent backend is ever added, integration tests should be added here
to verify the dispatch path works end-to-end for each backend.
