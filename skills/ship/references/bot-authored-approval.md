# Bot-authored MRs — the two approval prohibitions

Spine: [`skills/ship/SKILL.md`](../SKILL.md) § "The bot authors so the operator's account stays eligible".

Two prohibitions survive unchanged, and nothing in the spine weakens either:

- An approval recorded under the **BOT** identity is forbidden — the author cannot approve, and a self-approval is not review.
- An MR **authored by the owner's account** on such a repo is forbidden — it makes that account ineligible, leaving nobody who can approve.

A gate enforcing either is correct and stays; only its MESSAGE is worth fixing if it reads as an approval prohibition on the agent.
