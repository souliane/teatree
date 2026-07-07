---
name: t3-directive
description: "EVAL FIXTURE. Stand-in so a `/t3:directive ...` invocation resolves to a loadable Skill-tool catalog entry inside the isolated eval clean room. Captures the operator's directive verbatim via `t3 directive capture`. Placeholder only — see evals/scenarios/directive.yaml; the real skill is skills/directive/SKILL.md (the scenario's system prompt)."
---

# t3-directive (eval fixture)

Synthetic stand-in loaded only inside the teatree eval harness's isolated clean
room (`teatree.eval.api_runner`), so a scenario whose prompt starts with
`/t3:directive` resolves that invocation to a real, loadable Skill-tool catalog
entry instead of reading as an unknown slash command.

The one action is to record the directive text **verbatim**:

```bash
t3 directive capture "<verbatim directive text>" [--scope <overlay>]
```

Pass `--scope <overlay>` only when the directive names an overlay. With no
directive text, refuse in one line asking for it and run no command. This skill
only CAPTURES — it never edits code or drives the loop.
