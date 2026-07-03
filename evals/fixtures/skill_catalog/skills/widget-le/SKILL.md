---
name: widget-le
description: "EVAL FIXTURE. Stand-in for a hypothetical overlay's legal-entity review skill, part of the declared reviewer skill set for its legal-entity repos. Placeholder only — see evals/scenarios/skill_routing.yaml."
---

# widget-le (eval fixture)

This is a synthetic stand-in loaded only inside the teatree eval harness's
isolated clean room (`teatree.eval.api_runner`), so skill-routing scenarios
that reference `/widget-le` have a real, loadable Skill-tool catalog entry.
It carries no operational instructions and is never installed for real use —
core stays overlay-agnostic; an installed overlay supplies its own real
legal-entity review skill under its own name.
