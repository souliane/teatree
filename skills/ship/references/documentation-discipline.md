# Documentation discipline — the attestation examples and the deterministic gate

The recipes behind `/t3:ship` § "3a1. Documentation Discipline". That section carries the question every PR answers, the trigger-to-doc table, and the two runnable paths — a dispatched sub-agent receives only `SKILL.md`, so the commands it must be able to issue stay there. This file carries the `docs: n/a` attestation examples and how the deterministic gate divides the work with the prose.

The attestation line the NO path records reads:

```text
docs: n/a — <one-line reason>
```

Examples:

- `docs: n/a — internal refactor, no user-visible change`
- `docs: n/a — bug fix preserving existing contract`
- `docs: n/a — test-only change`
- `docs: n/a — generated-doc regeneration, source unchanged`

The line is the friction-free attestation. Reviewers read it; if the reason looks wrong they push back on the specific reason, not on a generic "did you update docs?" prompt.

**How the deterministic gate divides the work.** The unambiguous triggers (new top-level `t3` command, new `SKILL.md`, new `Ticket.State` value, new `LoopLease` name) are caught by `scripts/hooks/check_doc_update.py` automatically — the pre-push prek hook and the `doc-update-gate` CI job fail the push when the matching README/BLUEPRINT diff is missing. The skill prose in `/t3:ship` § "3a1. Documentation Discipline" handles the soft cases the hook cannot safely judge.
