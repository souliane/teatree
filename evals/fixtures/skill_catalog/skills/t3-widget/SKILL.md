---
name: t3-widget
description: "EVAL FIXTURE. Stand-in for a hypothetical overlay's multi-repo workspace playbook skill: worktree/run/test/review wiring for its repos. Placeholder only — see evals/scenarios/skill_routing.yaml and overlay_work_requires_overlay_skill.yaml."
---

# t3-widget (eval fixture)

This is a synthetic stand-in loaded only inside the teatree eval harness's
isolated clean room (`teatree.eval.api_runner`), so skill-routing scenarios
that reference `/t3-widget` have a real, loadable Skill-tool catalog entry.
It carries no operational instructions and is never installed for real use —
core stays overlay-agnostic; an installed overlay supplies its own real
workspace-playbook skill under its own name.
