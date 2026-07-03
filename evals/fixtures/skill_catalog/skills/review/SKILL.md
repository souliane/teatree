---
name: review
description: "EVAL FIXTURE. Stand-in for the code-review skill named without a leading slash by a scenario that must DERIVE it applies here rather than pattern-match a literal '/t3:review' in the prompt. Placeholder only — see evals/scenarios/skill_routing.yaml."
---

# review (eval fixture)

This is a synthetic stand-in loaded only inside the teatree eval harness's
isolated clean room (`teatree.eval.api_runner`), so skill-routing scenarios
whose prompt requires deriving the review skill (never spelling out
`/t3:review`) have a real, loadable Skill-tool catalog entry to call. It
carries no operational instructions and is never installed for real use —
the real project skill is `skills/review/SKILL.md`, shipped under teatree's
own `t3` plugin namespace.
