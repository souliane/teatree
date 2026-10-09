---
name: ac-python
description: "EVAL FIXTURE. Stand-in for the generic Python companion bible layered underneath a project's own backend dev skill for plain-Python (non-Django) work. Placeholder only — see evals/scenarios/skill_routing.yaml (overlay_python_coding_generalizes_to_python_bible)."
---

# ac-python (eval fixture)

This is a synthetic stand-in loaded only inside the teatree eval harness's
isolated clean room (`teatree.eval.api_runner`), so skill-routing scenarios
that reference `/ac-python` have a real, loadable Skill-tool catalog entry.
It carries no operational instructions and is never installed for real use —
core stays overlay-agnostic; a real environment supplies its own real Python
companion bible under its own name.
