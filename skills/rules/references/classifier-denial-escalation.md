# Classifier denial — escalation mechanics and the permissions boundary

The mechanics behind `/t3:rules` § "Classifier Denial Protocol (Non-Negotiable)". That section carries the decision — stop on a denial, re-issue only in an authorized form, escalate once through `AskUserQuestion`, and the banned reactions that are never taken. This file carries the Step 0 worked example, the settings-file edit procedure, the rationale, and the boundary of who relaxes permissions where.

## Step 0 in full — the denial reason names the in-scope form

**Step 0 — read the denial reason and check existing allow-rules before escalating.** The denial message states _why_ it was blocked, and that reason frequently names the in-scope form the action must take (e.g. "database outside the authorized `development-<tenant>` scope" → the authorized DB name is `development-<tenant>`, not the one you used). Before treating this as "needs relaxation": (a) parse the stated reason for the corrective scope, and (b) read the user's `~/.claude/settings.json` `autoMode.allow` and `permissions.allow` entries for a rule that already authorizes this action under the correct form. If either resolves it, the action was never out of policy — re-issue it in the **authorized form** (this is not a relaxation and needs no user prompt). Only if neither the reason nor an existing rule resolves it do you run the escalation. Skipping Step 0 and escalating a mere wrong-form mistake wastes the user's time on a decision they should never have been asked.

## The two options, and the settings-file edit

**Ask via `AskUserQuestion`** with two options:

- **"Allow it (relax classifier)"** — preferred. You then attempt the edit yourself (see below); only if the harness blocks the write do you fall back to a paste-ready snippet for the user to apply.
- **"Keep the denial (do it differently)"** — you propose a concrete alternative path (different tool, manual step, API call) and proceed only after the user picks one.

**If the user picked "Allow it":** attempt to add the rule to the user's `~/.claude/settings.json` (`permissions.allow` array) yourself, via the `Edit` tool. Read the file first, merge the new entry into the existing array, write it back. **If the write succeeds**, retry the original command. **If the write is denied** by the harness self-modification guardrail, only then fall back: hand over a paste-ready snippet, wait for the user to apply it, then retry. Do not preemptively skip the edit attempt — the goal is zero manual operations for the user when the harness allows it.

## Why this rule exists

The classifier exists to give the user a final say on standing-permission expansions. Auto-mode aggressiveness combined with classifier strictness is a recurring source of teatree workflow breakage — agents that retry, decompose, or sidestep silently accumulate scope, lose user trust, and ship work the user never authorized. The right escalation is to **ask once, fix permission at the user-scope settings file, retry**.

## Standing recommended set (proactive, not reactive)

This protocol governs _reacting_ to a mid-session denial. The _standing_ generic set of authorizations that prevents most denials in the first place — and the read-only `t3 doctor authorizations` check that suggests (never applies) the absent ones — is documented in [`skills/setup/references/recommended-automode-authorizations.md`](../../setup/references/recommended-automode-authorizations.md). That doc and the protocol do not duplicate: one is the standing recommendation, the other the in-session escalation.

## Boundary: who edits permissions where

- Teatree (the rules skill, BLUEPRINT, plugin `settings.json`) defines the _protocol_. Teatree never relaxes permissions on the user's behalf.
- The agent **attempts** the edit to `~/.claude/settings.json` (user scope) directly — that's the path with zero manual steps for the user. Many users have a standing authorization for this in their `autoMode.allow`. The agent only falls back to handing over a paste-ready snippet **after** the harness self-modification guardrail blocks the write — never as the default path. The snippet is the manual fallback, not the primary mechanism.
- Plugin-distributed permissions (`plugins/t3/settings.json`, `CLAUDE.md` standing clauses) are **never** the right place to relax for a single workflow — that would grant the standing right to every user of the plugin. Refuse if asked to do this; explain that user-scope `settings.json` is the right knob.
