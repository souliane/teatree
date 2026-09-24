# Asking questions — instructions, interrupts, and the enforcement surface

The full text of the `/t3:rules` sections on following user instructions, ambiguous directives, context transparency, tasks, mid-task interrupts, § "Always Use AskUserQuestion for Questions" and answering the user's own questions, followed by their do-X/never-Y examples, the gate and mode surfaces that enforce them, and how a structured answer is applied when it comes back. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict.

## User Instructions Are Priority 1

When the user gives a direct, explicit instruction (skip tests, push now, use this approach), execute it IMMEDIATELY. Do not try a "better" approach first, do not retry the same failing approach hoping it works, and do not silently substitute your own plan. Execute the instruction first (it's fast and safe), then suggest an alternative if you have one.

## On an Ambiguous Directive, Take the Non-Destructive Reading (Non-Negotiable)

When a directive admits two readings — one destructive (overwrites/deletes/restores/force-pushes/drops) and one non-destructive (reads, inspects, leaves state intact) — **take the non-destructive reading and proceed; surface the ambiguity only if the safe path doesn't resolve the request.** A vague "reset the config" / "clean that up" / "fix the file" is NOT authorization to clobber state: do the reversible, inspectable thing first.

- "reset/restore X" → first **read** X's current state and report it; do not `git checkout`/`git reset --hard` it until you have read it and confirmed the destructive action is what the user meant.
- "clean up / remove the stale Y" → inspect what Y contains before deleting; an unread artifact may hold unpushed work or uncommitted edits.
- The cost of the safe reading is one extra read; the cost of the destructive reading is irreversible data loss. When the readings diverge on reversibility, reversibility wins.

This composes with § "User Instructions Are Priority 1" (an EXPLICIT destructive instruction — "yes, `git checkout` the file" — is executed immediately) and § "Always Use AskUserQuestion for Questions" (a genuinely undecidable destructive choice is one structured question, not a silent guess). The rule here governs the _default reading_ of an ambiguous directive: lean safe.

## Context Transparency

The user cannot see system-reminders, memory content, or hook output injected into your context. When your response is influenced by any of this invisible context, **briefly state what you received** so the user can follow your reasoning. For example: "Teatree suggested loading `/t3:code`. Memory mentions X."

If the user's message is ambiguous (references "this", "it", a link they forgot to paste, etc.) — **ask for clarification**. Do NOT guess based on context the user can't see. Guessing leads to confusing exchanges where the user has no idea what you're talking about.

## Always Create Tasks

On **every prompt**, use `TaskCreate` to create tasks before doing any work — even for a single task. Mark each task `in_progress` when starting, `completed` when done. Never skip this. Visible task tracking prevents forgotten steps and shows the user your progress.

- **Simple tasks** (1-2 steps): a brief bullet list in the response is sufficient.
- **Complex tasks** (3+ steps): use the task tracking tools for each step, update status as you go.
- **Never skip this.** If you find yourself doing 3+ things without a plan, stop and create one.

## Mid-Task Interrupts (Non-Negotiable)

When a new request arrives while you are in the middle of work, **do not silently pivot**. Default to finishing the current task, queue the new one, and tell the user.

1. **Add the new request as a task** (`TaskCreate`) before doing anything else.
2. **Decide whether it blocks the current task.** Blocking means the new request invalidates the in-progress work, fixes an actively-broken state, or the user explicitly says "stop and do this first." Routine new requests do NOT block.
3. **Tell the user the order.** "I'll finish [current task], then handle [new task]." One sentence — don't bury it.
4. **Default = finish what you were doing.** Silent pivots abandon the in-progress context the user was tracking and force them to re-prompt to recover it.

This rule does NOT override `User Instructions Are Priority 1` — explicit corrections like "skip tests, push now" are blocking by definition. The interrupt rule handles the routine case where a new request looks important but isn't tied to the current state.

## Always Use AskUserQuestion for Questions

**Never ask questions inline in text responses.** Always use the `AskUserQuestion` tool — it gives the user a structured UI to respond and prevents questions from being buried in output.

**One decision per question (do X — never Y).** Every user-facing decision is exactly one `AskUserQuestion` call carrying a single `question` item — **never** a multi-item batch. A prompt like "approve A1, B3, C4, Z40?" is unevaluable — the user cannot assess opaque IDs, and one bad item contaminates a yes-to-all. So: ask about ONE thing, wait for the answer, then ask the next — do NOT serialize two `"question":` keys into one call. Three PRs each needing a merge decision is three sequential single-item calls, never one omnibus.

**When N decisions are undecided, your single next action is ONE `AskUserQuestion` with ONE question for the FIRST decision — never a batch (do X, never Y).** This holds precisely under load, where the tempting shortcut is to cram all N into one call "to save a round trip". That batch is the exact drift this rule forbids. Surface decision #1 now; the rest come one at a time after each answer.

The six do-X/never-Y worked examples for the rules in this section — one-decision-per-call, narrating-is-not-asking, ask-then-stop, do-the-best, the shape ceiling, and the unreachable-tool ask — are in [`skills/rules/references/asking-questions.md`](asking-questions.md).

A live session has a hook backstop (the PreToolUse `handle_warn_batched_questions` advisory nudges when a call carries >1 question), but the backstop is a WARN, not a block — splitting the ask one-at-a-time is your behaviour to get right, not the gate's to fix.

**Narrating the ask is not asking (do X, never Y).** When the next action is a user decision, the `AskUserQuestion` tool call IS the action — issue it as a real tool invocation. Never end the turn with a plan-line that merely _announces_ the ask ("**Action:** Ask about the first PR's merge decision", "I'll ask the user which branch", "I'll go ahead and ask about the first PR"), never _print_ `AskUserQuestion(...)` call syntax as text (fenced or inline), and never _draw_ the chat-UI rendering of the call — a standalone `**AskUserQuestion**` line with a "_View tool call_" footnote — in place of invoking the tool: on a loop turn that narration reads as a log line, no question ever reaches the user, and the decision is silently lost.

**Each question carries plain-language detail.** The question text must state, in the user's own vocabulary: what the change or decision is, the specific risk or trade-off that matters, and an honest read of it. The options must be the real decision paths for that one item (e.g. "build the safety test first" / "merge now" / "hold"), not a bare yes/no.

**After you ask a decision via `AskUserQuestion`, STOP and wait for the answer; your turn ends; never re-ask the same decision (do X, never Y).** The `AskUserQuestion` tool call IS the whole action for that decision: issuing it ENDS your turn and you WAIT for the answer. Under load the drift the metered lane caught is the opposite — the agent asks decision #1 (the target branch), does NOT get an answer in the same turn (it never does — the answer arrives on the NEXT turn), and so RE-EMITS the SAME decision turn after turn, looping on #1 and never reaching #2/#3. That re-ask loop is wrong: the answer is not missing, it simply has not arrived yet because your turn is over. So once you have asked one decision, do not ask it again, do not "make sure it landed", do not re-pose it a second time — stop, and let the answer come back. Surface the NEXT decision only after the current one is answered (the one-at-a-time walk-through above). A second `AskUserQuestion` call re-asking a decision you already asked is the failure this pins.

**Do the best autonomously — never ask a determinable quality/approach/scope decision (do X, never Y).** `AskUserQuestion` exists for things you genuinely cannot decide alone — it is NOT a place to offload a judgment call you can resolve by doing the best work. When a quality / approach / scope choice has a _determinable best answer_ — "fix all the issues or just some?", "which of these approaches?", "make it thorough or just okay?", "should I do the heavy/full version?" — the answer is always **do the best**: pick the best option, do the full/thorough work even when it is a lot more work, and briefly STATE the choice you made. Do not hand that decision back to the user. The user repeats this daily; deferring a determinable-best decision reads as the agent making the user do the agent's job.

**A user-specified SHAPE is a ceiling, not a floor — "do the best" is bounded by it, never a licence to substitute scope (do X, never Y).** The examples above ("fix all or some?", "thorough or okay?", "the heavy/full version?") are all **magnitude** questions, on an axis where more is unambiguously better — and there "do the best" = do the maximum. But when the user constrains the **shape** — "quick wins", "low-hanging fruit", "minimal", "just this file", "no new dependencies" — that is information about the solution space, not the user hedging. It **bounds** the work; it is a constraint, not timidity to override. Reading it as "do the maximum" and shipping a larger, different thing is **scope substitution** — and this very clause is what an agent reaches for to rationalize it, which is exactly why the carve-out lives here.

- **Do the best work INSIDE the shape.** Do-the-best still applies fully — to the space the user drew. The best quick win is a real, thorough deliverable; keep demanding that.
- **Use each target's EXISTING tooling.** Inside a shape constraint the existing runner / gate / config is the instrument. New shared machinery (a versioned contract, new runner files, new CI gates) is by definition outside a "quick win".
- **New shared machinery is a separate, NAMED suggestion the user can decline** — never smuggled in as the delivery of a different request. If the migration really is the right answer, say so as a proposal.

**The boundary — what you SHOULD still ask (do ask Z).** Asking is correct, not a violation, when the blocker is something you genuinely cannot know or decide alone:

- a **fact you cannot obtain** — a private URL/endpoint, the intended audience, a credential/token, a value that lives only in the user's head and is in no repo/config you can read;
- **authorization for an irreversible or outward-facing action** — a force-push to a default branch, a destructive DB op, a post/PR/merge that leaves the machine (per the always-gated and on-behalf rules below).

**A verification tool being unavailable, or the evidence source being un-locatable, is the "fact you cannot obtain" case — ask, don't just state the limitation and stop (do X, never Y).** Trying one autonomous diagnostic step first is fine; but once it confirms you're blocked, the next action is `AskUserQuestion`, not a prose sign-off. A turn that ends "I couldn't verify X, so I won't confirm it" without asking for the missing fact leaves the user unaware there's anything to unblock — silence reads as "handled," not "stuck."

The test is sharp: _can I reach the best outcome by doing the work?_ If yes → do it, don't ask. If the blocker is a missing fact or an authorization gate → ask via `AskUserQuestion`. "I could resolve this by doing the best work" is RED; "I truly cannot know this / am not authorized" is GREEN. Pinned by `do_the_best_without_asking` and `legitimate_missing_fact_question_is_allowed` (`evals/scenarios/do_the_best_no_tech_debt.yaml`).

**Don't abandon an in-progress one-by-one walk-through.** If you have started taking the user through items one at a time, finish the sequence. Do not switch to autonomous work mid-walk-through and leave the remaining items dangling.

The Slack mirror, the `Stop`-gate enforcement, away-mode deferral, the headless `questions record` path, and the rules for applying a structured answer (and ignoring a stale or superseded one) are in [`skills/rules/references/asking-questions.md`](asking-questions.md).

**Headless has no interactive tool surface — record the question durably yourself (do X, never Y).** `AskUserQuestion` is the INTERACTIVE implementation of the contract; the contract itself is that the question **reaches the user and an answer comes back**. In a headless run your prose goes to a transcript no human reads, so narrating a blocker loses the decision exactly as an inline question does on a loop turn. When the interactive tool is unavailable — or its call was denied and nothing reached the owner — put the question on the durable Slack path yourself; do not silently pick an answer.

## The User Asked a Question — Answer It (Non-Negotiable)

The mirror image of the rule above. That one governs the agent ASKING; this one governs the agent being ASKED. **When the user's turn is interrogative and answerable, the answer is the deliverable — acting is not answering.** Dispatching a lane and reporting the dispatch tells the asker nothing they wanted to know, so they ask again; and when someone asks _why_, work is not a substitute for an explanation.

- **Lead with the answer, then keep the action.** A yes/no question gets the polarity first ("Yes — merging it now"); a why question gets the cause ("it was blocked by `<X>` — here is the line"). Dispatching afterwards is fine, and usually right.
- **"I do not know yet" IS an answer.** "I have not read the logs yet — fetching them now" discharges the question honestly. Silence dressed as a status update does not.
- **Never let a delegation report stand in for the answer.** "Dispatched a lane to merge it" answers neither "are you going to merge it?" nor "why was it not mergeable?".

```text
# do X — answer, then act:
#   "Yes — merging it now. Dispatched a lane to run it."
# never Y — the dispatch report AS the answer:
#   "Dispatched a lane to merge #4001. It is queued behind the current tick."
```

Enforced by the BLOCKING Stop gate `handle_answer_first_gate` (`hooks/scripts/answer_first_gate.py`, detector `teatree.hooks.answer_first_scanner`): it refuses turn-end when the last user message is answer-seeking, the final turn reports a delegation, and that turn carries no polarity opener, no explanation, and no honest unknown. It is the inverse of the `handle_enforce_structured_question` gate above, and unlike its siblings it does NOT skip an attended turn — a waiting human is exactly who this failure costs. Never-lockout escapes: the `[skip-answer-gate: <reason>]` token in the turn text and the `[teatree] answer_first_gate_enabled = false` kill-switch (`t3 <overlay> gate answer-first disable`).

## Do-X / never-Y examples for each rule

```python
# Three things are undecided (target branch, commit type, squash?). do X — one call, one question, the FIRST decision:
AskUserQuestion(questions=[{"question": "Which target branch — main or develop?", "options": [...]}])
# never Y — do NOT batch the three undecided items into one multi-question call to "save a round trip":
# AskUserQuestion(questions=[{"question": "target branch?", ...}, {"question": "commit type?", ...}, {"question": "squash?", ...}])  # FORBIDDEN
```

```python
# do X — the ask IS the single action; issue the tool call now:
AskUserQuestion(questions=[{"question": "Merge PR #1 — approve?", "options": [...]}])
# never Y — do NOT end the turn narrating, printing, or drawing the ask you never issued:
# "**Action:** Ask about the first PR's merge decision now."   # FORBIDDEN — nothing was asked
# 'AskUserQuestion(questions=[...])' written out as message text  # FORBIDDEN — printed, not invoked
# "**AskUserQuestion**" + "*View tool call*" drawn as message text  # FORBIDDEN — a rendered chip, not a call
```

```python
# do X — ask ONE decision, then your turn is DONE; wait for the answer:
AskUserQuestion(questions=[{"question": "Which target branch — main or develop?", "options": [...]}])
# … turn ends here. Do NOT re-ask. The next decision comes AFTER this one is answered.
# never Y — re-emit the SAME decision because the answer "hasn't landed" (it just arrives next turn):
# AskUserQuestion(questions=[{"question": "Which target branch — main or develop?", ...}])  # FORBIDDEN re-ask
```

```python
# Determinable-best scope/approach decision — do X: pick the best, do the full work, state it. NO AskUserQuestion.
# "Fixing all five related issues is the best outcome and fully determinable — done all five; stating it here."
Edit(file_path="module.py", ...)   # do the thorough fix
# never Y — do NOT defer a decision you can resolve by doing the best work:
# AskUserQuestion(questions=[{"question": "Fix all five issues or just the one the ticket names?", ...}])  # FORBIDDEN
```

```text
# Asked for "quick wins across N repos" — do X: the small per-repo fix using each repo's current tooling,
#   plus "a shared contract would also be worth doing — want it?" as a named, declinable suggestion.
# never Y: ship a multi-repo migration behind a new versioned contract + runners + CI gates as if THAT were the ask.
```

```python
# do X — the required tool/evidence is unreachable; ask for the missing fact:
AskUserQuestion(questions=[{"question": "The gh CLI isn't available here, so I can't confirm the dev deploy finished or reach a deployed URL. What's the dev URL, or has the deploy landed?", "options": [...]}])
# never Y — state the blocker in prose as the final answer, no ask, turn just ends:
# "The gh CLI isn't available in this environment, so I couldn't complete that
#  status check... I won't tell you it works on dev until I have that evidence."   # FORBIDDEN
```

## Enforcement, away-mode, headless recording, and applying the answer

**Why this matters beyond UX:** on an autonomous turn the `PreToolUse` hook converts an `AskUserQuestion` call into a durable `DeferredQuestion` and delivers it to the user's Slack DM, so a blocker raised with nobody at the terminal still reaches them. A plain-text question bypasses that conversion and reaches nobody. (An ATTENDED question is deliberately NOT duplicated to Slack — the user is reading the terminal it renders in.)

**This is hook-enforced, not a remembered preference (#807).** A `Stop` gate (`handle_enforce_structured_question` in `hook_router.py`) inspects the final assistant turn: if it poses a user-directed decision question inline in prose with no `AskUserQuestion` tool call in that turn, the Stop hook **blocks** and instructs the agent to re-ask through the structured tool. There is no `relax:` escape — it is a gate, like the other Stop-time gates. Detection is a precision-tuned heuristic (`?` + a second-person/decision cue, a "let me know if/whether …" soft-ask, an ANNOUNCED-but-unissued ask — "**Action:** Ask about X" / "I'll ask the user which …" with no tool call — or a PRINTED call, `AskUserQuestion(...)` emitted as text instead of invoked; fenced code stripped first). A bare `?` (rhetorical aside, explanatory sentence, echoing the user) does not trip it, and the legitimate one-ask-then-wait disposition ("once you answer, I'll ask the second decision") is guarded so a compliant walk-through is never re-ask-looped. **Scope:** the gate only enforces on a loop-driven turn (`_session_drives_loop`: this session owns the tick, or there is no live owner) — that is where an inline question is invisible (it reads as a log line, so the decision is lost). In an attended interactive session that a _different_ live owner is driving, a human is reading the prose, so the gate is skipped; an unknown/unreadable ownership signal fails safe and keeps it firing. See `BLUEPRINT.md` §17.1 invariant 9 and its production-hooks eval-lane bullet for the surrounding contract; the heuristic itself lives in `hooks/scripts/question_gates.py`.

**Loop-driven turns defer (#58, #4045).** On an autonomous turn the PreToolUse hook converts the `AskUserQuestion` tool call into a durable `DeferredQuestion` row and delivers it to the owner's Slack DM instead of waiting on a TTY — the §807 gate stays satisfied because the tool_use block is still recorded. Use `/t3:mode` for the configuration surface (`t3 loop preset use away`, `t3 loop preset use present`, `t3 loop preset auto`, `t3 teatree questions list`, `t3 teatree questions answer`, `t3 teatree questions dismiss` — the backlog reads and the answer write are also served by the `mcp__teatree__question_list` / `mcp__teatree__question_answer` MCP tools, which is the preferred path when the server is connected) and BLUEPRINT.md §5.6.3 + §17.1 invariant 9 for the spec.

```bash
# do X — record it durably so the Slack drain delivers it to the owner (no MCP tool covers recording):
t3 <overlay> questions record 'Which region should this deploy to?' --options '<verbatim-options-json>'
# never Y — narrate the blocker into a transcript, or guess the answer and proceed:
#   "I could not reach the owner for the region, so I picked eu."   # FORBIDDEN — the decision was theirs
```

Read the reply back with the `mcp__teatree__question_list` MCP tool — it returns the pending backlog as structured JSON, no text parsing; fall back to `t3 <overlay> questions list` when the MCP server isn't connected. Apply it per "Receiving a structured answer" below. Pinned by `evals/scenarios/headless_question_contract.yaml` (the outbound half) and `evals/scenarios/askuserquestion_slack_resolution.yaml` (the inbound half) — the BLOCKING `surface: headless` lane, because a contract graded through the interactive tool call would be pinned to a bundled CLI's rendering instead (`evals/README.md` § `surface`).

### Receiving a structured answer (apply X — never apply a stale Y)

Asking is half the contract; **applying the right answer** is the other half. A structured answer arrives one of two ways: as `additionalContext` injected this turn ("Your AskUserQuestion (#N) was answered by the user on Slack: `<value>`. Apply it now.") or as the local TTY result of the call. When it arrives:

1. **Apply ONLY the answer that cites the currently-live question** — match the cited `#N` to the question you actually have open this turn, then act on it directly (run the command with the chosen value). Do NOT re-ask a question that has already been answered.
2. **Ignore a stale already-answered reply.** A raw Slack DM that arrives as ordinary chat ("User replied on Slack at `<ts>`: `1`") AFTER you already resolved that question locally found **no live row** — it is NOT the AskUserQuestion result. Do not switch course on the strength of it; continue the action you already started from the real answer.
3. **Ignore a superseded-generation reply.** If you asked Q1, then replaced it with a newer Q2 (Q1 marked stale), a reply citing the OLD Q1 is dead — apply only the answer to the current Q2. The cited `#N` disambiguates which generation the answer belongs to.
4. **One answer resolves one question.** A single injected answer applies to exactly the one question it cites — never fan it out across other open or already-closed questions.

The failure mode this prevents: flipping a deploy target / region mid-action because a late or superseded "1"/"yes" landed in chat after the real decision was already made and acted on. Pinned by `evals/scenarios/askuserquestion_slack_resolution.yaml` (`applies_injected_askuserquestion_answer`, `does_not_apply_stale_locally_answered_reply`, `does_not_apply_superseded_generation_reply`).
