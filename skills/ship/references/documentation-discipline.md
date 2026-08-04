# Documentation discipline — the trigger table, the two paths, and the attestation

The recipes behind `/t3:ship` § "3a1. Documentation Discipline". That section carries the question every PR answers; this file carries the trigger-to-doc table, the YES and NO paths, the `docs: n/a` attestation examples, and how the deterministic gate divides the work with the prose.

| Trigger | Doc to update |
|---|---|
| New `t3` command / flag / env var | `README.md` (user-facing usage) |
| New `Ticket.State` / FSM phase / `LoopLease` name | `BLUEPRINT.md` |
| New `SKILL.md` added (or one removed) | the top-level `README.md` skills catalogue |
| Skill behaviour change | the relevant `SKILL.md` |

```bash
# YES path — open the matching doc to add the entry (canonical HOW; e.g. a new SKILL.md):
$EDITOR README.md          # skills catalogue, or the user-facing command doc
$EDITOR BLUEPRINT.md       # for a new FSM state / lifecycle concept
```

**If NO:** the MR description carries this attestation line on its own — record it directly, do NOT touch README/BLUEPRINT:

```text
docs: n/a — <one-line reason>
```

```bash
# NO path — append the attestation to the PR body draft (canonical HOW):
echo "docs: n/a — <one-line reason>" >> .git/PR_BODY.md
```

Examples:

- `docs: n/a — internal refactor, no user-visible change`
- `docs: n/a — bug fix preserving existing contract`
- `docs: n/a — test-only change`
- `docs: n/a — generated-doc regeneration, source unchanged`

The line is the friction-free attestation. Reviewers read it; if the reason looks wrong they push back on the specific reason, not on a generic "did you update docs?" prompt.

**How the deterministic gate divides the work.** The unambiguous triggers (new top-level `t3` command, new `SKILL.md`, new `Ticket.State` value, new `LoopLease` name) are caught by `scripts/hooks/check_doc_update.py` automatically — the pre-push prek hook and the `doc-update-gate` CI job fail the push when the matching README/BLUEPRINT diff is missing. The skill prose in `/t3:ship` § "3a1. Documentation Discipline" handles the soft cases the hook cannot safely judge.
