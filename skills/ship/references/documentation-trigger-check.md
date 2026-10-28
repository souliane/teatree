# Documentation Discipline (Non-Negotiable)

Before creating the PR, ask: did this diff change anything a user or colleague would learn from the README, BLUEPRINT, or any skill file?

Common triggers (not exhaustive):

- New `t3` command, flag, or env var
- Renamed or removed public symbol, command, or setting
- New FSM state, lifecycle phase, or BLUEPRINT-keyed concept (e.g. a new `Ticket.State`, a new `LoopLease` row name)
- New `SKILL.md` added, or one removed
- User-observable behaviour change (default flips, UI flow, error message shape, response payload)
- New feature flag

**If YES:** the same MR includes the doc update — pick the file by the trigger, then start editing it before `pr create`:

```bash
# YES path — open the matching doc to add the entry (canonical HOW; e.g. a new SKILL.md):
$EDITOR README.md          # skills catalogue, or the user-facing command doc
$EDITOR BLUEPRINT.md       # for a new FSM state / lifecycle concept
```

**If NO:** the MR description carries this attestation line on its own — record it directly, do NOT touch README/BLUEPRINT:

```bash
# NO path — append the attestation to the PR body draft (canonical HOW):
echo "docs: n/a — <one-line reason>" >> .git/PR_BODY.md
```

The attestation examples and how `scripts/hooks/check_doc_update.py` splits the work with this prose are in [`skills/ship/references/documentation-discipline.md`](documentation-discipline.md).

Both layers (the gate and the attestation) run on every PR — the gate runs deterministically, the attestation is the reader's signal that the agent considered docs and made a deliberate call.
