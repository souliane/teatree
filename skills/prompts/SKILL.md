---
name: prompts
description: 'Trigger and manage reusable prompts — list the prompts in the DB, render one by name with its templated params, and point to the admin for authoring + version history. Use when the user says "prompts", "run a prompt", "trigger a prompt", "render a prompt", "list prompts", or "prompt library".'
eval_exempt: thin `t3 prompts` CLI reference; prompt render/versioning behaviour is covered by tests/teatree_core/models/test_prompt_params.py and tests/teatree_core/test_prompts_command.py, not by agent prose here
compatibility: any
requires:
  - rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Prompts — Reusable, Triggerable Prompts

A `Prompt` row is a named, reusable instruction — the durable home for prose that used to live in skill markdown (#2513). A prompt is triggerable on its own, carries declared templated params, and keeps a version history of every content edit. A `Loop` row may point at a prompt as its instruction (the loop XOR: a loop runs either an on-disk `script` OR a `Prompt`).

## When to load

Load `/t3:prompts` when the user wants to see, render, or trigger a reusable prompt — phrasings like "prompts", "run a prompt", "render a prompt", "list prompts", "prompt library".

This is a thin reference over the `t3 prompts` CLI. Authoring (creating prompts, editing the body/params, browsing version history) lives in the Django admin (`Prompt` rows).

## The commands

```bash
t3 prompts list              # every prompt: name, declared params, version depth, description
t3 prompts list --json       # the same as a machine-readable payload

t3 prompts render <name>                 # render a no-param prompt to its instruction body
t3 prompts render <name> --arg who=adrien --arg what=ship   # substitute declared params
```

## Render contract

- `render` substitutes ONLY the prompt's **declared** params (`{who}`, `{what}`) into the body. Any other `{...}` in the body (a JSON snippet, an example) is left literal — a prompt body is safe to carry braces.
- A **missing** declared param or an **undeclared** `--arg` is a loud error, never a silent wrong-render.
- A no-param prompt renders its body verbatim.

## Params + version history

- `params` is the list of declared templated-arg names a prompt's body templates over.
- Every content edit (body or params) snapshots the **superseded** content as a `PromptVersion` row, keyed on `(prompt, version)`. The edit history is durable and auditable — browse it under the prompt in the Django admin. An identical edit is a no-op (no version churn).
- Authoring goes through the model's `revise(body=..., params=...)` method (admin / management), which captures the prior content before writing the new — so a snapshot is never orphaned from its edit.

## Relationship to loops

A prompt-backed `Loop` runs its prompt's body as the per-tick instruction (e.g. "run a sub-agent to do X"). See `/t3:loops` for the loop side. The domain scanners under `teatree.loops` stay as the scan units a loop invokes — the prompt says *what* to run, not new behaviour.
