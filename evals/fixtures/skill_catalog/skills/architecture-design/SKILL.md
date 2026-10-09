---
name: architecture-design
description: "EVAL FIXTURE. Stand-in for the architecture/planning companion skill a planning scenario must self-load before drafting any plan. Placeholder only — see evals/scenarios/skill_routing.yaml."
---

# architecture-design (eval fixture)

This is a synthetic stand-in loaded only inside the teatree eval harness's
isolated clean room (`teatree.eval.api_runner`), so a skill-routing scenario
whose prompt tells the agent to self-load the architecture/planning skill has
a real, loadable Skill-tool catalog entry to call. Core's own skills are not
registered in the clean room's Skill catalog, so without this entry the call
comes back "Unknown skill" and the agent burns its whole budget hunting the
filesystem for a skill it can never load.

It carries no operational instructions and is never installed for real use —
the real skill is `skills/architecture-design/SKILL.md`, shipped under
teatree's own `t3` plugin namespace.
