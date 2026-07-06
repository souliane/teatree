---
name: directive
description: Submit a plain-English directive about how teatree itself should behave —
  captured verbatim, interpreted into a typed mechanism sketch, human-ratified via
  Slack/questions, then implemented through the gated pipeline. Use when the user says
  "/t3:directive ...", "directive:", "from now on teatree should...", "teatree must
  always/never...", or asks to change teatree's own behavior in natural language.
compatibility: any
requires:
  - rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Directive — Capture a Plain-English Self-Modification Request (Intake Only)

`/t3:directive <plain English>` is the explicit operator entry to teatree's
self-modification loop. It does exactly ONE thing: record the directive text
**verbatim** as a `CAPTURED` row via `t3 directive capture`, echo the captured id,
and set expectations. Everything after capture — interpret into a typed mechanism
sketch, ratify with the human, implement through the gates — runs headless on the
directive loop and is out of this skill's scope.

Capture is **always live**, even while every directive-loop flag is dark: the
explicit path (`Directive.objects.capture(source=CLI)`) records the row regardless
of loop state. Only the downstream processing is staged behind flags.

## The one action

```bash
t3 directive capture "<verbatim directive text>" [--scope <overlay>]
```

1. **Treat `$ARGUMENTS` as the VERBATIM directive text.** The ledger stores the
   user's own words (`raw_text` verbatim is a model invariant) — never paraphrase,
   summarize, or "clean up" the text before capture. Pass it through unchanged.
2. **Empty args → one-line refusal.** If there is no directive text, respond in one
   line asking for it (e.g. `Give me the directive text — what should teatree do?`)
   and run no command.
3. **Scope defaults to global.** Pass `--scope <overlay>` ONLY when the user names an
   overlay or the text unambiguously targets one. If the scope is genuinely
   ambiguous, ask exactly ONE `AskUserQuestion` (global vs the candidate overlay),
   then capture. When in doubt and no overlay is named, capture global.
4. **Echo the captured id + state in one line.** The command prints
   `captured directive #<id> (state=captured).` — surface that id back to the user so
   they can inspect it (`t3 directive status <id>`). A directive is a local ledger
   row, not a forge issue, so there is no URL to link — the plain `#<id>` is the ref.

## Set expectations (≤3 lines, after capture)

- Interpretation runs headless on the directive loop — you do not drive it.
- A **ratify question arrives as a Slack DM** (or read it via
  `t3 teatree questions list` / `/t3:checking`); answer it to approve.
- Nothing implements before that approval — `Directive.admit` is structurally
  human-gated (it raises without a consumed, answered ratify question).

If the directive loop is dark (its `Loop` row disabled or `directive_loop_enabled`
off — visible as a `SKIP` from `t3 directive tick`), still capture (the explicit path
is always live) and add ONE line: the ratify question will not arrive until the loop
is enabled, so the capture waits in the ledger until then.

## HARD boundaries (intake only)

This skill NEVER:

- interprets the directive or writes a mechanism sketch,
- edits code, config, or any file to satisfy the directive,
- answers its own ratify question (that is the human's headlight — maker≠checker),
- runs `t3 directive tick` (the loop's own cron entry advances the FSM, not you).

Capturing is the whole job. Interpretation, ratification, and implementation belong
to the loop and the human, not to the capture turn.

## Read-backs (on request)

```bash
t3 directive status <id>   # one directive's state, sketch, ratification
t3 directive list          # recent ledger (id, state, scope, text)
t3 directive history       # recent ledger with decisions
```
