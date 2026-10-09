---
name: frontend-dev
description: "EVAL FIXTURE. Stand-in for a hypothetical overlay's project dev skill (frontend flavour) — the standing companion skill loaded alongside the coding/reviewing phase skill for its frontend repos. Placeholder only — see evals/scenarios/skill_routing.yaml."
---

# frontend-dev (eval fixture)

This is a synthetic stand-in loaded only inside the teatree eval harness's
isolated clean room (`teatree.eval.api_runner`), so skill-routing scenarios
that reference `/frontend-dev` have a real, loadable Skill-tool catalog entry.
It carries no operational instructions and is never installed for real use —
core stays overlay-agnostic; an installed overlay supplies its own real
project dev skill under its own name.
