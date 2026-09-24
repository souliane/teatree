# On-behalf posting — signatures, the gate, modes, and chokepoints

The full text of `/t3:rules` § "No AI Signature on Posts Made on the User's Behalf", § "Ask Before Posting on the User's Behalf" and § "Never Post PR Comments from Parallel Agents", followed by the three mode values, the chokepoints that enforce them, and the scope boundary. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict.

## No AI Signature on Posts Made on the User's Behalf (Non-Negotiable)

Every artifact you publish under the user's identity — git commits, MR/PR descriptions, MR/PR comments and discussions, issue bodies, Slack/Teams messages, email drafts, release notes — must read as if the user wrote it. **Never append AI/agent signatures or footers**.

**Canonical setting:** `agent_signature` (DB-home, default `false`) — set with `t3 <overlay> config_setting set agent_signature <true|false>` (add `--overlay <name>` for the per-overlay scope). Programmatic teatree code paths that post on the user's behalf consult `teatree.identity.agent_signature_enabled()` (or wrap their suffix in `agent_signature_suffix(...)`). When you publish through an external tool (MCP Slack send, `gh` comment, `glab` discussion, raw `httpx`), apply the same policy by hand: omit the signature unless the setting is `true`.

**Banned trailers and footers in any user-on-behalf artifact:**

- `Co-Authored-By: <model> <noreply@anthropic.com>` (or any other agent identity) <!-- privacy-scan:allow a public vendor address named as a banned trailer -->
- `🤖 Generated with Claude Code` / `Generated with [Claude Code](...)`
- `Sent using Claude` / `Drafted by Claude` / `via Claude` / `(via AI)` / `via the assistant`
- Any emoji-bot signature or "this message was written by …" footer
- Slack-block "Posted by Claude" / "AI-generated" formatting

**This rule is global, not commit-specific.** The original "no Co-Authored-By in commits" rule was a special case; the principle generalizes to every venue where the agent posts on the user's behalf. If you would not put `Co-Authored-By` on a commit, do not put `Sent using Claude` on a Slack message. The user is responsible for the content; the agent is the typist, not the author.

**When the user is the author and explicitly invokes you:** if the user asks for a draft to review before sending themselves, no signature is needed (they will send it themselves anyway). When **you** post on their behalf (Slack DM, PR discussion, GitHub comment, email), the rule still applies — the message must be indistinguishable in form from one the user wrote.

**Failure mode this rule prevents:** the agent appends "Sent using Claude" to a Slack message it sends to a colleague on the user's behalf. The colleague now sees that the user did not write the message themselves; the user looks lazy or impersonal, and the rapport with the colleague is damaged. Same logic for `Co-Authored-By` in commits, "🤖 Generated" footers in PR descriptions, and "via the assistant" suffixes in issue comments.

## Ask Before Posting on the User's Behalf (Non-Negotiable)

**Canonical setting:** `on_behalf_post_mode` (DB-home, default `"draft_or_ask"`, per-overlay overridable) — set with `t3 <overlay> config_setting set on_behalf_post_mode <value>` (add `--overlay <name>` for the per-overlay scope). It takes three values.

The gate covers colleague-**VISIBLE** posts only. A **draft** (`post_draft_note`) is colleague-invisible — only the user can submit it — so it is **exempt under every mode** and never needs approval; that exemption is the whole point of the setting.

An **author-side reply on the owner's OWN MR** — `t3 review reply-to-discussion`, the only surface carrying the `reply_to_discussion` action, where the MR's author is one of the owner's forge identities — is likewise exempt: answering a reviewer on your own MR is your own voice on your own work, not a colleague-facing review post. Authorship is PROVED per call by `teatree.cli.review.own_mr.owner_authored_mr` and fails CLOSED — an unreadable MR, an author-less payload, or an unresolvable identity all keep the reply gated. The carve-out is scoped by BOTH the action and the authorship, so a reply on a **colleague's** MR, and every other on-behalf surface on your own MR (`approve`, `unapprove`, `post_comment --live`, `publish_draft_notes`, `resolve_discussion`, `update_note`, `delete_discussion`), stay gated exactly as before. The receipt DM still fires on every carved-out reply — the autonomy is the absence of a pre-ask, never of visibility.

`teatree.core.on_behalf_egress.OnBehalfSlackEgress` is the single owner of **every colleague-surface Slack post AND react** under the user's identity — review-DONE reactions, the `:merge:` reaction, broadcast outcome reactions, review-nag posts, the `notify post` / `notify react` CLI, and `t3 slack react`. All of them run gate→route→emit→audit in one place, so a colleague reaction can never slip past the gate; a self-DM short-circuits ungated, so a self-ack stays free.

The three mode values (`draft_or_ask`, `ask`, `immediate`), the verdict resolver, and the other two chokepoints are in [`skills/rules/references/on-behalf-posting.md`](references/on-behalf-posting.md).

When the verdict is `BLOCK`, before any post/comment/approval/reaction the agent makes **under the user's identity to a colleague or customer surface** — a GitLab/GitHub PR/MR comment, an issue comment, a PR/MR approve or unapprove, a Slack channel or thread message, a Notion page or comment, an emoji reaction on someone else's message — the agent must obtain the user's explicit approval **first** (via `AskUserQuestion` for ad-hoc agent posts, or by recording an `OnBehalfApproval` for teatree code paths — see below) and publish only after the user confirms.

How the gate is satisfied by a recorded `OnBehalfApproval`, what sits outside it, and the `notify_on_post_on_behalf` receipt are in [`skills/rules/references/on-behalf-posting.md`](references/on-behalf-posting.md).

**Which CLI to run — the DESTINATION picks the credential, you never name one.** Both shapes below route through `OnBehalfSlackEgress`, which classifies the destination and selects the credential itself: the user's own DM goes out as the overlay bot, a colleague or channel goes out under the user's own identity. So the command carries only a destination and a body. No teatree surface accepts a credential or an identity-switch flag — if you find yourself reaching for one, the command is wrong, not incomplete. For the self-DM, prefer the `mcp__teatree__notify_user` MCP tool — same audited egress, and a non-delivery returns a `reason` slug plus a human `detail` instead of a bare failure; the colleague/channel shape has no MCP tool and stays on the CLI.

```bash
# colleague channel (or a colleague's DM) — gated, then routed to the user's own identity:
t3 <overlay> notify post --channel <channel> --text '<message>'
# an emoji reaction on a colleague's message — the same gated egress:
t3 slack react --channel <channel> --ts <timestamp> --emoji <name>
# the user's OWN DM (bot→user self-DM) — exempt from the gate, never on-behalf:
t3 <overlay> notify send '<body>' --idempotency-key <key>
```

**Failure mode this prevents:** the agent posts a poorly-worded reply or an approval the user did not intend under the user's name to a colleague, and the user only learns of it after the fact (or via the notify receipt). The pre-gate keeps the user in control of their own voice until they choose to delegate it.

## Never Post PR Comments from Parallel Agents (Non-Negotiable)

MR/PR comment posting (test plans, evidence, review notes) must be **serialized** — never dispatch two parallel agents that both post comments on PRs. Parallel agents cannot check for each other's posts, resulting in duplicate comments.

**Serialized means one poster at a time — it does NOT mean the main agent posts directly (do X, never Y).** "Serialize" governs ordering, not who acts. The main/orchestrating agent is never the poster itself: per § "DISPATCH IMMEDIATELY — the orchestrate-only boundary" below, a colleague-visible publish (`t3 review post-comment`, `post-draft-note`, a test-plan or evidence comment) is dispatched to a single sub-agent, exactly like a code edit — the boundary is about WHO touches a colleague-facing surface, not about the call being short enough to "just do it here." Serialize by dispatching one sub-agent, collecting its result, then dispatching the next — never by having the main agent shortcut the dispatch and run the posting command itself in the foreground.

```python
# EXAMPLE — `my-org/my-repo` and `acme` are stand-ins, not a teatree target. Nothing here is a work item.
# do X — dispatch the single posting action to a sub-agent, then stop:
Task(description="Post review finding", prompt="Post an inline `t3 review post-comment` on my-org/my-repo!4120, src/acme/billing/sweep.py line 88: <finding text>. Report the comment URL.")
# never Y — the main agent runs the posting command itself because it's short/serialized:
# Bash(command="t3 review post-comment my-org/my-repo 4120 '<finding>' --file src/acme/billing/sweep.py --line 88")   # FORBIDDEN in the main agent
```

## The three `on_behalf_post_mode` values and the enforcement chokepoints

- `"draft_or_ask"` (default) — the agent produces a **draft** without asking (`post_draft_note` returns AUTO_DRAFT and the gate emits a bot→user DM with idempotency key `on_behalf_autodraft:{target}:{action}` so the user can review and publish). Every colleague-VISIBLE action (publish, comment, approve, reply, react…) is BLOCKED until the user records an approval.
- `"ask"` — identical to `draft_or_ask` for drafts (a draft still auto-publishes + DMs the user — it is exempt) and for colleague-visible posts (all BLOCKED until the user records an approval). `ask` does **not** block draft-note creation.
- `"immediate"` — gate is lifted: the agent publishes per the resolved `mode` doctrine without the pre-ask (the user has opted the overlay into trusted unattended posting).

Resolved by `teatree.on_behalf_gate.resolve_on_behalf_verdict(action, own_mr=…)` which returns `PASS` / `BLOCK` / `AUTO_DRAFT`. Enforced uniformly inside teatree at three chokepoints: `teatree.core.reply_transport._BaseReplier` (Slack thread reply / Slack channel / GitLab MR comment / GitHub PR comment), `teatree.cli.review.ReviewService` (`post_comment`, `post_draft_note`, `publish_draft_notes`, `reply_to_discussion`, `resolve_discussion`, `update_note`, `delete_discussion`), and `teatree.core.on_behalf_egress.OnBehalfSlackEgress` — the single owner of **every colleague-surface Slack post/react** under the user's identity (review-DONE reactions, the `:merge:` reaction, broadcast outcome reactions, review-nag posts, the `notify post`/`notify react` CLI, `t3 slack react`). `OnBehalfSlackEgress.post/.react` run gate→route→emit→audit in one place; a self-DM (the #1750 `route_token` classifier, fail-closed to colleague on an unknown surface) short-circuits ungated, so a colleague reaction can never bypass the gate while a self-ack stays free. The `PullRequest.approve()` → ✅ reaction signal and the ticket-transition emoji signal route through the same gate (on the separate `slack_reactions` transport).

## The author-side carve-out — the owner's own MR

`reply_to_discussion` on an MR the OWNER AUTHORED resolves to `PASS` under every mode: answering a reviewer on one's own MR is the owner's own voice on the owner's own work, not a colleague-facing review post, and the owner decided (2026-07-23) it posts autonomously — no draft-first, no per-reply approval.

Two independent conditions gate it, so neither half can widen it alone:

- **the action** must be in `teatree.on_behalf_gate._AUTHOR_SIDE_ACTIONS` (`reply_to_discussion` only). Passing `own_mr=True` for `approve`, `unapprove`, `post_comment`, `publish_draft_notes`, `resolve_discussion`, `update_note` or `delete_discussion` still BLOCKs;
- **the authorship** must be PROVED by the caller. `teatree.cli.review.own_mr.owner_authored_mr` reads the MR's `author.username` (through the same cached `shape_gate.fetch_mr_author` read) and matches it against the POSTING credential's own `current_username` unioned with `user_identity_aliases`. It fails **CLOSED** — an unreadable MR, an author-less payload, an unresolvable identity, or any transport failure all report `False`, which leaves the reply gated. This is the inverse polarity of its shape-gate sibling `is_colleague_mr`, which fails OPEN because it only relaxes a prose cap.

`own_mr` defaults `False` everywhere, so every other on-behalf chokepoint (`reply_transport`, `OnBehalfSlackEgress`, `review_request_post`, the test-plan posts) is untouched. `t3 review reply-to-discussion` is the only surface that proves and passes it. The `notify_on_post_on_behalf` receipt DM fires on a carved-out reply exactly as on any other on-behalf publish — the carve-out removes the pre-ask, never the owner's visibility.

## Satisfying the gate and what is out of scope

The gate is **satisfiable, not pure suppression**. The teatree code paths consult `teatree.core.on_behalf_gate_recorded.require_on_behalf_approval`, which mirrors the #953 `DbApproval` / §17.4 `MergeClear` shape: BLOCK verdict + a recorded, unconsumed, exactly-scoped `OnBehalfApproval` row → the post proceeds and the row is consumed single-use (an `OnBehalfAudit` row is written); BLOCK verdict + no recorded approval → the helper raises `OnBehalfPostBlockedError` and the caller surfaces the blocked post to the user — never silently dropped, never posted unattended. **No TTY is required** to satisfy it: a chat-only operator records the approval via `t3 review approve-on-behalf <target> <action> --approver <user-id>` and the next on-behalf attempt publishes. The factory refuses a maker/coding-agent/loop approver id (maker≠checker), so the executing agent can never self-authorize the post it is about to make.

- **Out of scope** (no pre-ask needed): DMs _to the user themselves_ (`Replier.post_dm`), the DailyDigest user thread, the `AskUserQuestion` Slack mirror, the bot→user notify path, and internal-only orchestration writes — our own teatree backlog issues, durable memory, task bookkeeping, the sanctioned `t3 <overlay> ticket clear` / `ticket merge` keystone. The bot→user self-DM is best sent with the `mcp__teatree__notify_user` MCP tool — same audited egress, and a non-delivery returns a `reason` slug plus a human `detail` rather than a bare failure; fall back to **`t3 <overlay> notify send <body> --idempotency-key <key>`** when the MCP server isn't connected. Neither is `notify post` (the gated colleague/channel path that routes via `OnBehalfSlackEgress` and requires `--channel` + `--text`).
- **Relationship to the notify-_after_ rule:** this is the _pre_-gate; the post-on-behalf notification is the _after_ receipt, now a real default-ON DB-home `UserSettings` field `notify_on_post_on_behalf` (default `true`, per-overlay overridable, **no env var**) — set it with the `mcp__teatree__config_setting_set` MCP tool (it accepts this key; pass `overlay` for the per-overlay scope), or `t3 <overlay> config_setting set notify_on_post_on_behalf <true|false>` when the MCP server isn't connected. After every colleague-visible on-behalf publish, `teatree.core.on_behalf_post_receipt.notify_user_on_behalf_post` DMs the user the destination, a clickable artifact link, and a one-line summary (recorded in the `BotPing` ledger; record-and-proceed — it never blocks or rolls back the post). This durable enforcement **retires** the per-session memory `notify-user-on-every-post-on-behalf` (souliane/teatree#949). Both ship on. The user widens `on_behalf_post_mode` per-overlay (`"draft_or_ask"` → `"immediate"`) once confident the system posts well via `config_setting set on_behalf_post_mode immediate --overlay <name>` — CLI-only, since `config_setting_set` refuses this safety-posture key by design; set `notify_on_post_on_behalf` to `false` per-overlay independently — the notify stays on longer.
- **Backward compatibility:** the legacy `ask_before_post_on_behalf` boolean is retired — under the #1775 partition its old `[teatree]` TOML key is ignored on read. Use `on_behalf_post_mode` (DB-home): `t3 <overlay> config_setting set on_behalf_post_mode <value>`.

The two runnable CLI shapes — the gated colleague-channel post and the exempt bot→user self-DM — stay in the `/t3:rules` § "Ask Before Posting on the User's Behalf" body, not here: a dispatched sub-agent receives only `SKILL.md`, so a command it must be able to issue cannot live in a reference file.
