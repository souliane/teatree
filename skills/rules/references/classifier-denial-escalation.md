# Classifier denial — escalation, predictable gates, and the permissions boundary

The full text of `/t3:rules` § "Classifier Denial Protocol (Non-Negotiable)", § "Anticipate a Predictable Gate" and § "Re-Derive the Minimal Blocker", followed by the Step 0 worked example, the settings-file edit procedure, the rationale, and the boundary of who relaxes permissions where. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict.

## Classifier Denial Protocol (Non-Negotiable)

When the auto-mode classifier denies a tool call (Bash command rejected, MCP call refused, "permission denied" from the harness, etc.), **stop immediately**. Do not retry, do not work around it with a different command, do not "find another way". A classifier denial is an **immediate session blocker** — handle it before doing anything else.

**Step 0 — the denial reason usually names the in-scope form; re-issue in it rather than escalating.** Parse the stated reason for the corrective scope, and check the user's `~/.claude/settings.json` `autoMode.allow` / `permissions.allow` for a rule that already authorizes the action under the correct form. If either resolves it, the action was never out of policy — re-issue it in the **authorized form**; that is not a relaxation and needs no user prompt. Escalate only when neither resolves it: escalating a wrong-form mistake wastes the user's time on a decision they should never have been asked.

**Required response when Step 0 does not resolve it:**

1. **Stop.** Drop whatever you were doing. Do not start an alternative approach in the same response.
2. **Inform the user** in plain text: which command was denied, what you were trying to accomplish, and the smallest static permission rule that would have allowed it (e.g. `Bash(gh issue create *)`, `Bash(docker buildx prune *)`). The rule must be the smallest rule that covers the use case — never a blanket `Bash` or `Bash(* *)`.
3. **Ask via `AskUserQuestion`** with exactly two options: **"Allow it (relax classifier)"** — the preferred one — or **"Keep the denial (do it differently)"**, for which you propose a concrete alternative path and proceed only after the user picks one.
4. **If the user picked "Allow it":** attempt the `~/.claude/settings.json` edit yourself via `Edit`, then retry the original command. A paste-ready snippet for the user to apply is the fallback **after** the harness blocks that write, never the default path.
5. **Wait for the answer.** Do not retry the denied command, do not invent workarounds, do not file tickets, do not start unrelated work, until the user has chosen and (if relaxing) the new rule is in place.

**Banned reactions to a classifier denial:**

- Silently retrying with a different argument shape hoping the classifier passes (`gh issue create` → `gh api repos/.../issues`).
- Switching tools (Bash → MCP, MCP → Python subprocess) to bypass the rule.
- Decomposing the command into pieces that each pass individually.
- Editing teatree's plugin `settings.json`, `CLAUDE.md`, or any plugin-distributed permissions file to add an allow rule — the user-scope `settings.json` is the only right knob, and teatree never relaxes permissions on the user's behalf.
- Continuing the surrounding work and "leaving the denial for later".

The Step 0 worked example, the settings-file edit procedure, the rationale, the standing recommended authorization set (and the read-only `t3 doctor authorizations` check that suggests it), and the full permissions boundary are in [`skills/rules/references/classifier-denial-escalation.md`](classifier-denial-escalation.md).

## Anticipate a Predictable Gate: Offer Enable-Setting or Approve-Once, Never Bypass-or-DIY (Non-Negotiable)

Gates — the on-behalf pre-gate, the E2E gate, the merge/CLEAR keystone, the auto-mode classifier — are an **extra safety net, not the primary control**. Ideally teatree never hits one. When you can foresee that the next action **will** hit a gate (a colleague-visible on-behalf post while `on_behalf_post_mode` is `ask`/`draft_or_ask`, a merge that needs a recorded human approver, a command no `autoMode.allow` / `permissions.allow` rule covers), surface the choice to the owner **proactively, BEFORE hitting the block** — do not blunder into the gate and only then react.

The choice you offer is **solution-oriented** and has exactly two options:

1. **Enable the setting durably** — flip the standing knob so the friction is gone for good (`t3 <overlay> config_setting set on_behalf_post_mode immediate`, add the `permissions.allow` / `autoMode.allow` rule, record the standing authorization).  <!-- mcp-ratchet: allow — on_behalf_post_mode is a safety-posture key the MCP config tool refuses, so the CLI is the only path -->
2. **Approve just this once** — a single-use, scoped authorization for exactly this one action (`t3 review approve-on-behalf <target> <action> --approver <user-id>`, `t3 <overlay> ticket e2e-bypass <id> --approver <user-id> --head-sha <sha>`).

**Never** frame the choice as "**bypass the gate, or do it yourself**". That pair is wrong on both sides: _bypass_ rips out the safety net with nothing durable in its place, and handing the whole action _back to the user_ is the very friction the enable-setting option exists to remove. Offering bypass-or-DIY is the anti-pattern this rule bans — asking it means you waited for the gate instead of anticipating it.

This composes with the Classifier Denial Protocol above: that section governs _reacting_ to a denial already hit; this one governs _anticipating_ a predictable block one action ahead, so the reactive path is rarely needed. Enforced in code by `teatree.core.on_behalf_gate_recorded.format_on_behalf_block_message` (the block message names both solution-oriented options, never a bypass) and `teatree.on_behalf_gate.on_behalf_post_will_block(action)` (the proactive pre-check that predicts the block before the post). Pinned by `proactive_gate_offers_enable_or_approve_once` and `proactive_gate_anticipates_before_hitting_not_bypass_or_diy` in `evals/scenarios/proactive_gate_doctrine.yaml`.

## Re-Derive the Minimal Blocker

When an operation is blocked — a classifier denial, a failing gate, an external or human-gated wait — re-derive the **minimal** set of work that genuinely depends on that exact operation before declaring anything else blocked. A blocked merge does not block PR creation, implementation, review, or research; a blocked deploy does not block the next feature. Before reporting "nothing actionable", ask of each pending task: does it consume the blocked operation's output, or does it merely share a goal (or sit later in the same chain) reachable by a different, available path? Reporting "nothing actionable" for two or more cycles behind a single external block is itself the signal to audit for a non-blocked path rather than continue idling. This complements the Classifier Denial Protocol (which governs the denied operation itself); this rule governs not over-propagating that block to independent work.

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
