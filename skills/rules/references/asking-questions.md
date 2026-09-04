# Asking questions — worked examples and the enforcement surface

The mechanics behind `/t3:rules` § "Always Use AskUserQuestion for Questions". That section carries the rules; this file carries their do-X/never-Y examples, the gate and mode surfaces that enforce them, and how a structured answer is applied when it comes back.

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
