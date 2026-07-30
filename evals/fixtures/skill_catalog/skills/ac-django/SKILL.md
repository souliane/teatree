---
name: ac-django
description: "EVAL FIXTURE. Stand-in for the generic Django companion bible layered underneath a project's own backend dev skill for Django work. Placeholder only — see evals/scenarios/skill_routing.yaml (overlay_django_coding_loads_companion_bible)."
---

# ac-django (eval fixture)

This is a synthetic stand-in loaded only inside the teatree eval harness's
isolated clean room (`teatree.eval.api_runner`), so skill-routing scenarios
that reference `/ac-django` have a real, loadable Skill-tool catalog entry.
It carries no operational instructions and is never installed for real use —
core stays overlay-agnostic; a real environment supplies its own real Django
companion bible under its own name.
