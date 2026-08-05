# Leak remediation — banned words, required form, and the pre-done grep

The catalogue behind `/t3:rules` § "Leak Remediation — Silent Scrubs" and § "Public-Repo Commit Author Identity". Those sections carry the rule that every public remediation artifact is neutral; this file carries the word list, the per-artifact required form, and the grep that checks it.

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
