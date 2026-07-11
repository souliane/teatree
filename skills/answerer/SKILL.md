---
name: answerer
description: Draft a reply to an inbound question, DM the user for approval, post on confirmation. Use when the loop routes a `question` intent to phase `answering`, or when asked to answer a Slack/GitLab thread on the user's behalf.
compatibility: macOS/Linux, git, chat/issue-tracker integration (Slack, GitLab, GitHub).
requires:
  - workspace
  - rules
  - platforms
  - verification-before-completion
metadata:
  version: 0.0.1
  subagent_safe: false
---

# t3:answerer — Draft, Get Approval, Post

## Delegation

This skill reuses `verification-before-completion` (from [obra/superpowers](https://github.com/obra/superpowers), optional) for the final post/no-post gate.
TeaTree keeps the rest locally because thread-context reading, autonomy gating, and idempotent posting are project workflow rather than generic doctrine.

From "an inbound question was routed to `answering`" to "a reply is posted on the user's behalf." The agent never posts an answer the user hasn't seen unless the user has explicitly opted into direct posting for the active overlay.

## Dependencies

- **t3:workspace** (required) — provides environment context. **Load `/t3:workspace` now** if not already loaded.
- **t3:rules** (required) — § "Publishing Actions Are Mode-Conditional" and § "No AI Signature on Posts Made on the User's Behalf" govern every post this skill makes.
- **t3:platforms** (required) — chat/issue-tracker recipes for reading the thread and posting.

## Input

The loop tick scanner classifies an `IncomingEvent` and the event router routes a `question` intent to `schedule_task` with phase `answering`. This skill picks up that task. The relevant record is the `IncomingEvent`:

- `event.source` — `slack`, `gitlab`, `github`, `notion`, `ci`
- `event.channel_ref` — channel / MR / issue the question came from
- `event.thread_ref` — the thread to reply in (may be blank). For a thread
  reply this equals the parent/root ts, so posting with it answers in the
  SAME thread, not as a new root message.
- `event.parent_ts` — the replied-to (parent/root) message's ts when this
  event is a thread reply; blank for a root message (`event.is_thread_reply`).
- `event.parent_text` — the parent message's text/snippet, resolved by the
  loop scanner so the referent is available without a second fetch. A reply
  like "where is the URL?" whose parent asked "approve posting the evidence?"
  resolves to the evidence artifact, not an unrelated ticket (#2230).
- `event.actor` — who asked
- `event.body` — the question text
- `event.id` — the database PK; the basis for the idempotency key

The dispatcher (`teatree.loop.dispatch`) routes the `answering`-phase
`incoming_event.task_needed` signal to this skill (`t3:answerer`) with a
statusline mirror, exactly like the reviewer dual-dispatch. It also
resolves `require_human_approval_to_answer` once (active-overlay →
global → default, mirrors `require_human_approval_to_merge`) and stamps
it into the agent payload as an advisory convenience mirror. The
autonomy gate below is the source of truth: always re-resolve the
setting at task start (the payload stamp is a hint, not authoritative).

## Autonomy Gate (read first)

The autonomy level is the per-overlay-overridable setting
`require_human_approval_to_answer` (`teatree.config.UserSettings`),
mirroring `require_human_approval_to_merge`.

- **`true` (default — draft + DM for approval).** Draft the answer, DM the
  user for approval, and post **only** on explicit confirmation. This is
  the safety belt for `mode = "auto"` overlays: the loop autonomously
  drafts but stops short of speaking on the user's behalf.
- **`false` (opt-in — post directly).** The agent posts the drafted answer
  without a DM round-trip. A deliberate opt-in the user makes per-overlay
  only once comfortable with answer quality, by raising the overlay's
  trust tier — `t3 <overlay> autonomy set notify` (or `full`), which
  collapses `require_human_approval_to_answer → False` for that overlay
  (the single homogenizing knob; never hand-edit config).
  Overlays whose questions are customer-facing or high-stakes should keep
  the tier at `babysit`.

Resolve the effective value with
`teatree.config.get_effective_settings().require_human_approval_to_answer`
(active-overlay override → global → default, exactly like every other
entry in `OVERLAY_OVERRIDABLE_SETTINGS` and mirroring
`require_human_approval_to_merge` — there is no env-var layer for this
setting). Never hard-code the behaviour; always read the resolved
setting at the start of the task.

In `interactive` mode every publishing action prompts regardless — the
setting only changes behaviour for `auto` overlays. See
[`../rules/SKILL.md`](../rules/SKILL.md) § "Publishing Actions Are
Mode-Conditional".

## Workflow

### 1. Read the Thread Context

Before drafting, read the surrounding thread so the answer is grounded in
what was already said — not just the one message that triggered the
classification. Use the backend matching `event.source`. See your
[chat platform reference](../platforms/references/) and
[issue tracker platform reference](../platforms/references/) for the
recipes (Slack thread fetch, GitLab/GitHub MR/issue comment thread).

- Pull the full thread, not just `event.body`. When the event is a thread
  reply (`event.is_thread_reply`), `event.parent_text` already carries the
  replied-to message — anchor the referent on it before re-reading the
  thread, so a deictic reply ("where is the URL?", "is it ready?") resolves
  against what it actually answers.
- Note any prior answer already posted in the thread — do not duplicate it.
- **Never download or transcribe the bot's OWN audio attachment.** The
  Slack-TTS feature attaches a synthesised `speech.m4a` to the bot's own DM
  messages — a spoken copy of text already present in the same message.
  Re-transcribing it is pure token waste for zero new information. The Slack
  read surface strips it for you (`strip_self_audio_attachments`); if you
  ever see a `speech.m4a` on a bot-authored message, skip it. Audio on a
  message authored by the **user** (a voice note) is genuine new content —
  transcribe that.
- If the question needs information you cannot find (a decision only the
  user can make, missing context, ambiguous ask), do **not** guess. Draft
  a clarifying-question reply instead, or escalate to the user via DM.

### 2. Draft the Answer

Write a concise, accurate reply in the user's voice.

- Plain, humble tone — see the global writing-tone rule. No marketing
  phrasing, no positioning the user as smarter than the asker.
- No AI signature. See [`../rules/SKILL.md`](../rules/SKILL.md) § "No AI
  Signature on Posts Made on the User's Behalf". The user is the author;
  the agent is the typist. Never append "Generated with", "Sent using",
  `Co-Authored-By`, etc.
- Match the channel: short and conversational for Slack; structured for an
  MR/issue comment.

### 3. Autonomy Branch

Read `get_effective_settings().require_human_approval_to_answer`:

**`true` — DM for approval (default):**

1. DM the user via `Replier.post_dm` with the drafted answer and a one-line
   summary of the question + where it will be posted.
2. Idempotency key: `f"answer-approval:{event.id}"` — a retried tick
   re-sends the same DM at most once (the reply transport collapses
   duplicates on the key).
3. Wait for an explicit approval reaction/reply from the user. Edits the
   user requests are folded into the draft; re-DM only if the change is
   substantive.
4. Only on explicit confirmation, proceed to step 4. **No confirmation →
   no post.** Silence is not approval.

**`false` — post directly (opt-in):**

Skip the DM and proceed straight to step 4.

### 4. Post the Answer

Post via the `Replier` method matching the channel:

- Thread reply (Slack thread, MR/issue discussion):
  `Replier.post_in_thread(event=event, target_ref=event.channel_ref,
  thread_ref=event.thread_ref, body=..., idempotency_key=...)`
- Top-level comment (no thread): `Replier.post_comment(...)`

Idempotency key: `f"answer:{event.id}"` — derived from
`IncomingEvent.id`, so a retried tick or a redelivery never double-posts
the same answer. The reply transport short-circuits on a duplicate key.

### 5. Record & Report

- The reply transport records the `ReplyDispatch` outcome
  (`sent`/`failed`) with the failure's `error_message` — retry and
  dead-letter handling belong to the transport layer, not the answerer;
  do not hand-roll retries here.
- Report back: the question, the channel, whether approval was required
  and obtained, and the posted-message reference.

## Rules

- **Default is draft + DM.** Never post an answer the user hasn't seen
  unless `require_human_approval_to_answer` is explicitly `false` for the
  active overlay.
- **Silence is not approval.** When approval is required, post only on an
  explicit confirmation. No reply → no post.
- **One answer per event.** The idempotency key is derived from
  `IncomingEvent.id`. Never post the same answer twice; never invent a
  fresh key to "force" a re-post.
- **No AI signature.** Posts made on the user's behalf carry no agent
  identity. See [`../rules/SKILL.md`](../rules/SKILL.md).
- **Don't guess.** If the question needs a decision or context you don't
  have, draft a clarifying question or escalate — never fabricate an
  authoritative-sounding answer.
- **Don't duplicate.** Check the thread for an existing answer before
  posting.
- **Read the setting, don't hard-code it.** Always resolve
  `get_effective_settings().require_human_approval_to_answer` at task
  start.

## Extension Points

Project overlays can override these behaviours:

| Extension Point | Default | Override Example |
|---|---|---|
| `require_human_approval_to_answer` | `true` (draft + DM) | Per-overlay `false` for a low-stakes internal overlay |
| `answer_channel_routing` | Reply in the originating thread | Route certain question types to a triage channel |
| `answer_review_dm_target` | The configured user DM | A shared approvals channel for a team overlay |
