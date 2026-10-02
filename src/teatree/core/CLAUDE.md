# teatree core — local conventions

See the root [`CLAUDE.md`](../../../CLAUDE.md) for the code-quality bar and how to run things. This file adds only what is specific to `src/teatree/core/`.

- **Overlay-agnostic.** Core never imports or hard-codes an overlay. Overlay behaviour is reached only through the `OverlayBase` ABC (`overlay.py`) — its `get_*` extension hooks are the contract. Adding a hook means giving every registered overlay a default and updating each registered overlay (see `/t3:rules` § "Teatree Extension Point Changes Must Update All Registered Overlays").
- **Models live in the `models/` package**, one model per module (`ticket.py`, `worktree.py`, `session.py`, `pull_request.py`, …). `Ticket` carries the session FSM — add lifecycle states/transitions on the model, not in callers.
- **Composition over mixins** (root bar) is load-bearing here: `OverlayBase` composes `OverlayConfig` + `OverlayMetadata`; follow that shape for new overlay surface.
- **Overlay system + FSM spec:** BLUEPRINT.md § 6 (Overlay System). Don't re-derive the contract from a single module — read § 6 before changing the ABC or the state graph.
