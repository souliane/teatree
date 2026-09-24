# Filing and leaks — visibility, triage, observed facts, silent scrubs, commit identity

The full text of the `/t3:rules` sections on publishing to external repos: visibility checks, `needs-triage`, observed versus inferred, § "Leak Remediation — Silent Scrubs" and § "Public-Repo Commit Author Identity", followed by the word list, the per-artifact required form, and the grep that checks it. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict.

## Verify Repo Visibility Before Filing External Issues (Non-Negotiable)

Before creating an issue, PR, discussion, or any body of content on an external repo, **check the target repo's visibility**:

```bash
gh repo view <owner>/<repo> --json visibility,isPrivate
```

If the target is **PUBLIC**, the body must not contain internal identifiers: customer names, internal GitLab/Jira/Notion URLs, client-specific repo names, ticket IDs from private trackers, CI job/pipeline IDs, local filesystem paths (`/Users/…`, `/home/…`), environment variable values, or internal hostnames. Replace with generic placeholders (`<repo>`, `<namespace>`, `<ticket_url>`, `$T3_WORKSPACE_DIR/<ticket>/<repo>`) before posting.

**Ambiguous destinations need a question.** When the user says "file a bug" without a repo and there are multiple candidates (public upstream vs. private overlay, team repo vs. personal repo), use `AskUserQuestion` to confirm the target before writing the body. Never guess — the cost of asking is low; the cost of publishing internal info is high.

**The authorization to "file a bug" does not authorize posting internal info to a public repo.** User instructions like "file a teatree bug" authorize the _action_ of filing, not the _destination_. A public target always requires a scrubbed body.

## Self-Apply `needs-triage` on Agent-Filed Issues (Non-Negotiable)

`needs-triage` is a maintainer-review gate: the autonomous loop's issue-implementer claim path filters out any open issue carrying it (`IssueImplementerScanner` skips it at selection time, before the claim), so the factory never starts an issue the maintainer has not cleared.

The complication is that the factory files its own backlog issues **as the maintainer's own account** (e.g. `souliane`). The auto-apply GitHub Action keys on the issue author, so it cannot distinguish a human maintainer's issue from an agent-filed one — both look like the maintainer. The author-only Action therefore can't gate agent-filed issues on its own.

The convention closes that gap: **an agent self-applies `needs-triage` by default on anything it files that is not a direct user implementation order.** Concretely:

- When the agent files an issue autonomously (a backlog item surfaced mid-session, a retro/review enforcement gap, a research finding), include `needs-triage` in the labels.
- Omit `needs-triage` only when the user explicitly directed the implementation (the issue is the tracking record for work the user asked for now).
- Teatree's programmatic filing path encodes this: `FilingContext.auto_filed` defaults to `True` and adds `NEEDS_TRIAGE_LABEL`; a user-directed caller sets `auto_filed=False`.

When in doubt, apply `needs-triage` — a withheld issue costs the maintainer one label-removal; an un-withheld one risks the factory implementing something the maintainer never decided to build.

The label governs an issue that is going to exist. Whether it should exist at all is decided one step earlier by the backlog-reuse precondition — search the open backlog, extend a suitable host rather than adding a near-duplicate, one issue per root cause — which is canonical in `AGENTS.md` § "Issue Creation" and is not restated here.

## A Filed Issue Separates OBSERVED From INFERRED (Non-Negotiable)

A root-cause claim in a filed issue is load-bearing: the next reader starts from it, so a wrong mechanism sends them down the wrong path and costs more than filing no mechanism at all. Keep the two apart in the body, labelled:

- **Observed** — the commands run, their verbatim output, the states read, the `file:line` inspected. Reproducible: anyone re-running it gets the same thing.
- **Inferred** — the mechanism you believe connects those observations. A hypothesis until an experiment separates it from the alternatives.

Then:

- **An inference written in the observed voice is a claim you did not make.** "X fails because Y", when all you saw was X, reads to the next person as a measured fact.
- **When the mechanism rests on a SINGLE observation, say so and name the experiment that would confirm it.** One observation is consistent with several mechanisms; naming the discriminating experiment turns a guess into a next step.
- Stated uncertainty is cheap; a confidently wrong root cause is not — and it is invisible, because a plausible mechanism is never questioned again.

This is the published-artifact sibling of § "Re-Validate a Reused Guard in a New Destructive Context" and its "mark every load-bearing premise VERIFIED or UNVERIFIED" clause: that rule scopes to a sub-agent BRIEF, this one to anything that leaves the machine.

## Leak Remediation — Silent Scrubs (Non-Negotiable)

When remediating a privacy leak on a public repo (force-push to drop PII, delete a comment that exposed a credential, rewrite a branch that leaked internal data), **every public artifact produced during the remediation must be neutral**. Do not name what leaked, do not name that a leak occurred, do not describe the scrub. Announcing the remediation on a public surface amplifies the leak (Streisand effect) — the commit subject, the PR comment, and the branch name are all crawled, cached, and indexed.

## Public-Repo Commit Author Identity (Non-Negotiable)

Commits pushed to a PUBLIC repo (`souliane/*`) must have an author **and** committer email that is a GitHub noreply address — `<id>+<login>@users.noreply.github.com` (e.g. `21343492+souliane@users.noreply.github.com`). A real/deliverable address (any customer/personal domain inherited from local `.git/config` or the XDG global) in public history is a permanent PII leak that GitHub's own "block pushes that expose my email" does **not** catch for third-party domains. The accepted shape is the noreply pattern itself — not one hardcoded login — so any GitHub identity passes and any real email blocks. Private overlay repos are exempt. Enforced deterministically by the pre-push gate `scripts/hooks/refuse-public-push-with-leak.sh` (#730): on a violation it blocks and prints the offending identity plus the `git filter-branch --env-filter` rewrite to the repo's GitHub noreply identity; re-push after the metadata-only rewrite. <!-- privacy-scan:allow the GitHub noreply shape the rule prescribes -->

The banned-word list, the required form for each public artifact (commit subject, branch name, PR-close comment, push description), and the pre-done grep that checks them are in [`skills/rules/references/leak-remediation.md`](references/leak-remediation.md).

## Banned words, required form, and the pre-done grep

**Banned words in any public artifact produced during remediation** (commit subject/body, PR or issue description or comment, release note, changelog, public branch name):

`leak` / `leaked` / `scrub` / `redact` / `real` (as in "real phone number") / `private` / `personal` / `sensitive` / `accidental` / `accident` / specific classes of the leaked data (`phone`, `email`, `password`, `token`, `credential`, `secret`, `key`, `address`, `ssn`).

**Required form:**

- **Scrub commit subject:** neutral verb only. Good: `fix(<scope>): update example values`, `refactor(<scope>): replace placeholder`, `docs(<scope>): refresh example`. Bad: `scrub real phone number`, `remove leaked credential`, `redact personal email`.
- **Remediation branch name:** no signal. Good: `fix/update-examples`, `chore/refresh-docs`. Bad: `fix/scrub-phone-leak`, `hotfix/leaked-token`.
- **Closing a remediation PR:** prefer no comment at all. If one is required, keep it to the shortest neutral phrasing (`Superseded.` / `Not needed.`). Do not explain _why_.
- **Public push description:** same rule. No class-of-data words.
- **Secure explanations** (to GitHub Support, to the user, to incident response) belong only in the corresponding private channel — never in git history or public comments.

**Pre-done grep** (run before claiming the remediation is complete):

```bash
git log --format='%H %s%n%b' <branch-start>..HEAD | \
  grep -iE 'leak|scrub|redact|real|private|personal|sensitive|accident|phone|email|password|token|credential|secret|address'
```

Also grep every PR/issue body or comment authored during remediation. Any hit is Streisand — rewrite the artifact (or delete the comment) before declaring done.

**Why:** A commit subject is as public as the diff, and a PR-close comment is permanent. Describing what was removed tells the next reader exactly what used to be there and where to look in the commit graph. The fix is silent.
