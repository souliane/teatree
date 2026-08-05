# Recurrence escalation — the two retro tooling lanes

The command procedures behind `/t3:retro` § "Recurrence → Escalation". That section carries the classification rule; this file carries the two lanes that turn a recurrence into a tracked gate.

## `t3 <overlay> retro review-findings <pr-url>`

When the recurrence source is a PR's review comments, the deterministic scaffold does the bookkeeping so a class-C finding reliably becomes a tracked gate (the meta-gap this routing addresses):

1. Run `t3 <overlay> retro review-findings <pr-url>` with no `--classification`. It fetches the review comments through the forge client, computes a stable per-finding fingerprint, and lists each finding (marking any fingerprint already recorded on other PRs as `(recurring)` — the strongest class-C signal).
2. Classify each fingerprint A / B / C yourself after reading the diff and the existing gate set — the command never guesses the verdict, because "is this already enforced?" and "is this recurring?" need judgement the scaffold can't reliably automate. Write the verdicts to a JSON file mapping `fingerprint -> {"class": "C", "enforcement": "<smallest gate/test/hook>"}`.
3. Re-run with `--classification verdicts.json`. The command records every verdict to a durable per-PR store and files one scoped, banned-terms-safe, clickable-link enforcement issue per class-C finding — deduped by fingerprint, so re-running never refiles. A/B findings file nothing.

The emitted summary (per-class counts + filed-issue links) is the escalation record for the persistence summary in `/t3:retro` § "Persistence First".

This is the durability-in-tooling-not-vigilance principle applied to retro itself: an already-failed behavioral rule failing again is a signal to escalate the *level* of the fix, not to repeat the *same* level.

## `t3 <overlay> retro gate-failures`

When the recurrence source is a **quality gate firing on the agent's own output** — the inline-question Stop gate, comment-density, banned-terms, the doc-update gate — the gate-failure feedback loop turns the firing into an eval that stops it firing first-try. In the on-disk session transcript a gate BLOCK is a `hook_blocking_error` attachment whose `blockingError` text leads with a `TEATREE GATE — <phrase>` marker (it carries no `exitCode`; `hookName` is the `Stop` / `PreToolUse:Bash` bucket, never a gate name).

1. Run `t3 <overlay> retro gate-failures` (latest in-scope session) or `--file <path.jsonl>` / `--session <id>`. It reads the single transcript hook-event chokepoint, keys on the attachment type + marker (excluding the `TEATREE LOOP SELF-PUMP` continue-signal), classifies each `preventable` (agent-output-shaped — should never have been produced) or `environmental` (a `hook_non_blocking_error` infra/dependency breakage — a missing plugin dir, a hook-runner traceback — an eval can't change the outcome), records each to the durable store, and lists them with the recurring mark.
2. For a **preventable + recurring** failure, add or improve the matching AI eval so the agent's first-try output passes the gate. The eval must be **anti-vacuous**: its `_fail` fixture (a transcript reproducing the violating output) goes RED. The canonical example is the near-zero-comments tendency: the `comment_density_writes_sparse_code` scenario (`evals/scenarios/code.yaml`) asserts the agent does NOT write a code-restating comment when adding a small function, so the comment-density gate stops being hit by trial-and-error.
3. Run with `--escalate --repo <slug> --pr-url <url>` to file one scoped, deduped enforcement issue per recurring preventable failure (fingerprint-deduped, banned-terms-safe, clickable-link safe — re-running never refiles). Environmental and non-recurring failures file nothing.

Privacy: the recorded `GateFailure` carries only the bounded gate-identity slug + the session id — never the blockingError message, the `stderr`, the `command`, or `stdout` (the diff/banned content). See `evals/README.md` § "Gate-failure feedback loop".
