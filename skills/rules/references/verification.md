# Verification — evidence, diagnosis, and honest claims

The full text of the `/t3:rules` sections on proving a claim before making it: completion evidence, diagnoses that cite what was read, falsifiable acceptance criteria, cross-reference coverage, loud external-read failures, canonical sources, deployed-environment evidence, honesty escalation, and re-validating a reused guard. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict.

## Verification Before Completion (Non-Negotiable)

_Adapted from [superpowers/verification-before-completion](https://github.com/obra/superpowers)._

**No completion claims without fresh verification evidence in the same response.** If you haven't run the command and read its output in this message, you cannot claim it passes.

1. **Identify** — what command proves this claim? (tests, lint, build, manual check)
2. **Run** — execute it fresh and completely
3. **Read** — full output, check exit code, count failures
4. **Claim** — state the result WITH evidence

**Banned language without evidence:** "should pass", "probably works", "seems correct", "looks good", "I'm confident". These words without a command output are lies, not claims.

**Read the state the claim is ABOUT, never a local proxy for it.** Step 3 says read the output; this says read the right thing. A claim about what LANDED is settled by reading the pushed commit, the remote, or the deployed surface — never the working tree, which goes on showing your edit whether or not it travelled. The recurring shape: a correction made after `git add` never reaches the commit, because the pre-commit runner stashes the unstaged change, commits the INDEX, and restores afterwards — so the file on disk still looks right and a local look "confirms" a fix that is absent from the remote history. Reasoning correctly about that mechanism is not the read. Name the read that settles it — `git show origin/<branch>:<path>` — and where you cannot run it yet, say the status is unsettled until you have.

**On a squash-merging repo, a sha-ancestry probe answers the landed question WRONGLY — read the content (do X, never Y).** A squash-merge rewrites the branch's commits into a new commit on the default branch, so the original sha is an ancestor of nothing and every per-commit / ancestor / range test reports successfully-landed work as absent.

```bash
# do X — the content IS the answer, and `-S` also dates it (was this live when <thing> ran?):
git show origin/main:<path> | grep -n '<symbol the change added>'
git log -1 -S'<symbol>' -- <path>
t3 <overlay> workspace branch-verdict <branch>   # the whole-branch question, one call
# never Y — these answer by sha or ancestry, which a squash-merge defeats:
git merge-base --is-ancestor <sha> origin/main   # FORBIDDEN as a landed-ness test
git log origin/main..<sha>                       # FORBIDDEN — same question, range spelling
```

The reverse direction is a DIFFERENT question and stays fine: `git merge-base --is-ancestor origin/main HEAD` asks whether main has reached your branch (currency), which no squash defeats.

**Multi-deliverable tickets: measure done from the SPEC, not the artifacts you produced (Non-Negotiable).** On a ticket with more than one deliverable, a completeness assertion — "done", "no blockers anywhere", "everything is here", "ready to merge/review" — is measured from **every deliverable the authoritative spec defines (incl. the spec's comments) verified on the actual merge target**, never from the artifacts you happen to have in hand. The recurring, highest-severity failure: claiming "no blockers anywhere" while the crucial deliverable was registered on the wrong surface and its fix was stranded off the merge target — invisible to a check that only inspects what exists. A false completion claim that propagates downstream is not an internal slip. Before any completion claim on a multi-deliverable ticket:

1. **Read the authoritative spec and its comments first.** A claim emitted before the spec source was read leans on proxies (the work item, repo docs, the baseline). If you have not read the spec, you cannot claim done.
2. **Enumerate EVERY spec deliverable** — not just the MRs/PRs created.
3. **Attach on-target evidence to EACH** — merged to the merge target / verified on the correct surface / passing E2E. "An MR exists" is NOT evidence.
4. **Verify the crucial/authoring deliverable explicitly on its correct surface** — the one that silently degrades to the wrong surface.
5. **Any deliverable lacking on-target evidence → say "NOT done: <X> missing / on the wrong surface / stranded off target"**, never "done".

This is enforced, not just prose: the BLOCKING Stop gate `handle_completion_claim_gate` (#2665) refuses turn-end on a multi-deliverable completion claim with no complete on-target deliverable→evidence map. It fires only on loop-driven turns; a legitimate single-deliverable "done" or a complete on-target map is never blocked. Never-lockout escapes: the `[skip-completion-gate: <reason>]` token in the turn text and the `[teatree] completion_claim_gate_enabled = false` kill-switch (`t3 <overlay> gate completion-claim disable`). This is the hard-blocking sibling of the WARN-only closure-reverify advisory (#1448).

**The answer to a gate rejection is evidence, not concessions (Non-Negotiable).** When the gate above — or any blocking gate, hook, or reviewer — rejects a claim, re-derive the assessment from the evidence. Do **not** go looking through your own correct work for something to concede so the pushback has an answer. Inventing defects is a worse failure than the over-claim the gate was catching: it corrupts every future self-assessment, and acting on a fabricated finding causes real damage (retracting a sound artifact, "fixing" correct text into something wrong). Concretely:

- A rejection means _"prove it"_, not _"find something wrong"_. Answer with the deliverable→evidence map; where a deliverable genuinely lacks on-target evidence, say so — and leave every other verdict untouched.
- Before reporting a defect in your own artifact, **quote the exact text and name the concrete failure**. If the quoted text does not actually exhibit the flaw, there is no finding — drop it.
- Keep the severity vocabulary honest: a **conflict** contradicts the spec; a **gap** is uncovered scope; an **optional extra** is a side note someone flagged as nice-to-have. Reporting a gap or a side note as a conflict inflates severity and invites a needless retraction.
- **In a bug report, "Actual Behavior" states the DEFECT, not the target.** Evidence and test plans demonstrate the _Expected Behavior_; never judge them for disagreeing with the Actual section. Misreading those two inverts the entire review.

## A Diagnosis Cites What Was Read (Non-Negotiable)

The rule above demands evidence for a COMPLETION claim. The same requirement applies one step earlier, to the DIAGNOSTIC claim — **a statement about why a system failed, and any escalation of a finding, must cite the artefact you actually read.**

The failure this prevents is fluent, not sloppy: a well-formed cause assembled from check NAMES, stated with the confidence of a diagnosis, by an agent that never opened the log. A colleague opens the log.

- **Quote the line.** "CI failed because X" needs the excerpt, the `file:line`, the rule or exception code, or the job link. One citation is the whole cost.
- **An unread hypothesis is labelled as one.** "I have not opened the logs; my guess is the ratchet fired" is honest and welcome. The same sentence without the hedge is an invention.
- **Severity waits for the evidence.** Escalating above a threshold — `SEVERE`, `CRITICAL`, `BLOCKER`, `P0` — requires the artefact that establishes it, not a relayed symptom. If your own brief asked for that evidence, the alarm waits for it to come back; report the symptom at its real confidence until then.

```text
# do X — the diagnosis carries what settled it:
#   "#4001 failed on one real violation: `ticket.py: 523 LOC, up from 510 (over the 500 cap)`."
# never Y — a plausible cause generated from the check names, log unopened:
#   "#4001 failed because 466 files were meeting the gates for the first time."
```

Enforced by the BLOCKING Stop gate `handle_unbacked_claim_gate` (`hooks/scripts/unbacked_claim_gate.py`, detector `teatree.hooks.unbacked_claim_scanner`): it refuses turn-end on a causal failure diagnosis, or a severity label, with no citation anywhere in the turn — and on a severity label whose own turn says the settling evidence has not come back. An explicitly-hedged diagnosis never fires. Never-lockout escapes: the `[skip-evidence-gate: <reason>]` token in the turn text and the `[teatree] unbacked_claim_gate_enabled = false` kill-switch (`t3 <overlay> gate unbacked-claim disable`).

## An Acceptance Criterion That Cannot Fail Is Not a Criterion (Non-Negotiable)

`/t3:code` § "TDD Discipline" mandates observing every regression test RED before trusting its green. This is that rule one level up, applied to feature acceptance: **before building against a criterion, name the state of the world that makes it FAIL.** If no such state exists, it is not a criterion — it certifies whatever you do, including doing nothing.

- **Absence-satisfied criteria are the highest-risk shape.** "X stays unchanged", "Y is untouched", "no regression in Z", "suite S still passes unmodified" are all satisfied by never touching the module — so skipping the work scores as success, and the skipped phase reports complete.
- **Pair every absence-satisfied criterion with a positive one only the implemented feature can satisfy** — a behaviour observably absent before the change and observably present after. The positive criterion is what the phase is verified against; the absence one is a guard, never the proof.
- **Say so when a handed-down criterion is unfalsifiable.** It is a defect in the spec, not a licence to satisfy it cheaply — surface it and add the positive pair before implementing.

The E2E-scoped statements of the same principle are `/t3:e2e` § "Writing Tests" (author side) and `/t3:e2e-review` § "Test the ticket, not the MR diff" (reviewer side): a test built against the diff's current behaviour passes regardless of whether the feature is correct. This section is the general form — apply it to acceptance criteria; they apply it to tests.

## Grep Before Claiming Cross-Reference Coverage (Non-Negotiable)

When the user asks how their codebase or harness compares to an external reference — an article, a framework's docs, a competitor's product, a popular library — the reflex is to pattern-match: a name in the reference resembles a skill or file or function the agent has seen, so the agent claims it's covered (or claims the inverse). This pattern-match is unreliable across naming differences and partial-implementation gaps, and it always defaults toward overclaiming coverage when the agent has the user's project context loaded.

**Required before any "X is covered / X is a gap" claim:**

1. **Grep the actual repo** for the concept under at least two framings. `rg`, `grep -r`, or `git log -S` against the codebase, not against memory.
2. **Cite `file:line`** for each "covered" assertion. A claim that something exists must point at where it exists.
3. **Cite the specific gap** for each "missing" assertion. Name the function, regex, or section that would have to exist and link the file path where you'd expect it. If you can't, you don't have enough evidence to call it a gap.
4. **If you can't grep** (no read access, ambiguous naming, the concept is implementation-shape rather than keyword-shape), **ask the user** before making the claim. Do not paper over the uncertainty with hedge words.

**Banned shortcuts:**

- Naming a skill ("/t3:code", "/t3:ship") and asserting it covers an article concept on the strength of its description alone.
- Listing items as "covered" because they sound like things the harness probably does.
- Producing a "what's missing" list without grepping for each item first.

**Why this rule exists.** When the user's project state is loaded into context (CLAUDE.md, MEMORY.md, recent file reads), the agent's pattern-matching defaults aggressive — it treats name-similarity as coverage and produces flattering comparisons that don't survive a `rg` check. The corrective is to require evidence at the point of claim, not at the point of correction.

## External Read Failure Must Fail Loud, Never Silent-Empty (Non-Negotiable)

An external / third-party read that FAILS — a missing MCP connector, an absent API token, a forge/API error, a down service — must **fail loud**: raise or surface the failure. Never degrade a read _error_ to an empty/degraded result that a caller then consumes as truth. A confidently-empty answer manufactured from a read failure is the trap — "no open PRs" / "no in-flight work" / "no findings" returned because the read errored, indistinguishable from a genuine empty, leads the caller to proceed on data it does not actually have. Silent-empty is forbidden.

- **A read ERROR ≠ a genuine empty.** "The forge returned zero rows" and "the forge read raised" are different outcomes; do not collapse them into the same empty return. Surface the error so the consumer cannot mistake a failure for "nothing there".
- **A sanctioned fallback transport is fine — a silent-empty is not.** Compatible with the #1192 fallback-transport pattern: a _deliberate, known_ fallback (a configured secondary channel, an explicitly-chosen local-only mode when a dependency is _absent by configuration_) is legitimate and may proceed. What is banned is laundering a _read failure_ into an empty result with no loud signal. The distinction is the seam: "no connector configured" (a known state → sanctioned degraded path) versus "the configured connector's read errored" (a failure → fail loud).
- **Fail-open at the ORCHESTRATION layer is a separate, explicit choice.** A caller may legitimately decide "this read must not block me" and catch the loud failure to continue (e.g. intake must not be blocked by a transient forge outage). That is a conscious, local decision at the _caller_, not a license for the _reader_ to hide the failure behind an empty return. The reader fails loud; the caller decides what to do with the failure.

Enforced in code by `teatree.core.intake.landscape_gather.run_landscape`, which raises `LandscapeForgeReadError` when a _configured_ forge's read errors (rather than returning a degraded-empty survey), while the _no-host-configured_ sanctioned fallback still degrades to a local-only survey.

## Read the Canonical Source Before Fixing a Conformance Bug

When a bug's root cause is "our code disagrees with an external authority" — a CI validator, a wire protocol, a spec, a sibling service's schema, an upstream library's behaviour — **read that authority's actual source before writing the fix or the red test**, not after. The fix for a conformance bug is _parity with the authority_, so the authority's exact behaviour (regexes, normalization, edge cases, what it does and does NOT check) is the specification. Implementing from the symptom or from an assumed root cause produces a fix that re-diverges differently: a discarded implement-and-test cycle, then a re-implement against the source that should have been read first.

- Locate the authority's source (vendored copy, sibling repo under the workspace, pinned dependency, the CI job's invoked script) and read the relevant function end to end.
- Derive the red test from the authority's behaviour, not from a hypothesis about it. If the authority does NOT enforce the thing you assumed, the bug is elsewhere — discover that before coding.
- Prefer vendoring the authority verbatim (pointer comment + drift-detecting parity test) over hand-reimplementing its rules, so future divergence is caught mechanically rather than by the next incident.

## Re-Verify Cross-Agent State Before Reporting a Dependent Request

In a multi-agent / multi-loop environment, another agent may have advanced a shared artifact (a PR merged, an issue closed, a branch rebased, a baseline moved) while your task was running. Before reporting a request or recommendation whose validity depends on that artifact's state ("dispatch a reviewer for PR N", "merge X next", "rebase Y"), **re-fetch the artifact's current state in the same turn you report it**. A request built on the artifact's state at task-start is stale by the time a long task finishes; reporting it makes the agent look out of sync and wastes the coordinator's turn correcting it. The cost of one `gh pr view` / `glab mr view` before the report is trivial; the cost of a stale dependent request is a wasted round-trip.

## Evidence Comes From the Deployed Environment (Non-Negotiable)

Before posting any screenshot, PDF, or "proof it works" artifact on an MR/PR/issue, **load `/t3:e2e`** and follow § "Evidence Source Integrity". The short version that every agent must remember even without the full skill loaded:

- **Required:** browser screenshots from the deployed dev/staging URL, OR documents regenerated on the deployed environment after merge + deploy.
- **Prohibited:** golden test PDFs from `build/test-results/` or `src/test/resources/`, `pdftotext` from a local build, screenshots of `localhost`, **and side-by-side comparisons assembled from PDFs extracted at different git commits**.

A passing local test suite is not evidence. The deployed system is the only artifact that proves a user-visible feature works. If the proper evidence requires steps you can't complete this session, don't substitute a prohibited source — and don't just narrate the limitation in prose and stop, either. This is exactly the "fact you genuinely cannot obtain" case in § "Always Use AskUserQuestion for Questions" § "The boundary — what you SHOULD still ask" below: while you're still mid-turn with the user/orchestrator (nothing posted yet), surface the blocker with `AskUserQuestion` — ask for the deployed dev/staging URL, or whether the deploy has actually finished — rather than declaring "I can't verify this" as your final answer with no way for the user to unblock you. Only once you're recording the outcome durably on the PR/MR/issue itself does the unavailability get written into the comment as the fallback note.

**When the deployed URL is missing, the `AskUserQuestion` call IS the single action.** Ask one structured question for the deployed URL or deploy-completion fact, then STOP and wait. If neither a concrete URL nor a concrete deployment/overlay identity is present in the context, do not invent or placeholder one for a probe. Never simulate that question with Bash — no `echo` of question-shaped JSON, placeholder status command, or unrelated worktree/CI probe reaches the user or supplies deployed evidence. Invoke the real question tool that is available; do not narrate or print the question through a command.

**The mandatory-E2E gate is bypassed ONLY by a recorded user approval — never by the agent self-asserting a skip.** For a display-impacting change that genuinely cannot get E2E this session, the single sanctioned escape is the user-authorized bypass command:

```bash
t3 <overlay> ticket e2e-bypass <ticket-id> --approver <human-user-id> --head-sha <full-40-char-sha>
```

It is durable, single-use, and scoped to the ticket + reviewed head SHA; the next ship-gate / §17.4 CLEAR at that exact SHA consumes it once. Maker≠checker is enforced — a `--approver` that is a maker / coding-agent / loop id is refused (#1967), so the implementing agent can never authorize its own bypass. There is no `--skip-e2e` flag and no `approve-on-behalf` path for the E2E gate; `ticket e2e-bypass` with a human approver is the only one. Conversely, once a green run's evidence is POSTED, record the attestation with the `mcp__teatree__record_e2e_run` MCP tool (same seam, same gate), or `t3 <overlay> lifecycle record-e2e-run <ticket-id> --spec <path> --result green --head-sha <sha> --posted-url <evidence-url>` when the MCP server isn't connected — a run recorded WITHOUT the posted evidence URL does not clear the gate.

## Escalate Honesty-Critical Verification to the Most-Honest Model

When ANY of these holds, record an honesty escalation **before the next verification/review/grading spawn**, so that work routes to the most-honest configured model (`[agent] honesty_model`, default Opus — requires no operator opt-in):

1. the user explicitly asks you to be honest;
2. you judge you have been dishonest;
3. the user accuses you of lying or of having "successfully failed" a task;
4. you shipped a job you cannot verify is complete.

Record it with:

```bash
t3 <overlay> honesty escalate --reason <user_asked|self_assessed_dishonest|accused_of_lying|shipped_incomplete> [--task <id>]
```

The escalation is **situational and auto-clears** — it is NOT a standing reviewer-model change. It is session-scoped, idempotent (re-firing the same trigger is a no-op), and bounded by a 6-hour safety-net TTL; the primary clear is an honest, verified-complete landing (a fully-passed rubric grade). Rationale: models learn honesty over time, so the most-honest model is the right one to _verify_ a moment the agent's own honesty is in question. The firing is yours to judge (it is prompt-level, SDK-portable — not a CLI-only flag); the consequence (raise → auto-clear) is deterministic. Trigger #4 also has a deterministic backstop: when the rubric done-gate refuses a merge, it records the `shipped_incomplete` escalation for you.

**Pick the escalation (and any per-stage override) target deliberately — cost is the operator's decision, not the agent's default.** Teatree carries no standalone "most expensive model" kill-switch: the per-phase/per-tier routing (`model_tiering.py`'s `TIER_MODELS`/`TIER_EFFORT`) is already explicit-opt-in, so nothing routes to a costlier-than-frontier model unless the operator names that model id themselves in config. This applies equally to a teatree Workflow's `model:`/`effort:` per-stage override (resolved DIRECTLY by the Workflow runtime, not through `model_tiering`) — pick the model per stage deliberately, and default to the resolved `honesty_model` / phase tier rather than reaching for whatever the most expensive available model happens to be.

## Re-Validate a Reused Guard in a New Destructive Context

A guard or classifier that is safe for gating one action is not automatically safe for authorizing another. Before reusing an existing guard to authorize a NEW destructive operation (`git reset --hard`, force-delete, force-push, `DROP`/`DELETE`), re-validate that the guard's safety property actually holds for THIS operation — e.g. subject-matching is sufficient to clean up a forge-merged branch, but only content-equivalence (patch-id) is safe before resetting a branch. In a sub-agent brief, never assert a safety property you have not verified; require the implementer to prove the property holds in the new context, and route any change that introduces a destructive operation through an adversarial review that specifically attacks the data-loss path.

**Mark every load-bearing premise VERIFIED or UNVERIFIED — not just a safety property (Non-Negotiable).** A safety property is one kind of premise a brief leans on, but the premises that break work are usually ordinary ones: an impact / LOC estimate ("~40 lines across 3 files"), "X is unused — a free delete", "the upstream lacks capability Y", "the blocker is Z". If the brief's plan changes when the claim is false, the claim is **load-bearing** — treat it exactly like a safety property. The failure this prevents is specific: an agent that COMPLIES with a false premise produces confident wrong work — the brief says "X is unused", the implementer deletes X, and every step after is locally correct and globally worthless. This slips past every other guard, because a false premise does not BLOCK anything: the sub-agent returns a clean success envelope (§ "Sub-Agent Limitations" fires only on a block), and the premise was handed DOWN to it, so it has no reason to grep its own claim (§ "Grep Before Claiming Cross-Reference Coverage" covers only the agent's OWN outward claim).

- **Mark each load-bearing premise `VERIFIED` or `UNVERIFIED`.** `VERIFIED` carries its evidence inline — a `file:line` or command output. `UNVERIFIED` is permitted and honest, and it carries an instruction: **check this first, before building on it.** The point is not to ban uncertainty; it is to stop uncertainty arriving disguised as fact. The `2x` estimate and the "free delete" both die at the marking step, before any work is built on them.
- **The implementer DISCONFIRMS AND STOPS — never routes around a false premise.** When a premise proves false on contact, the correct action is to stop and report the disconfirmation — not to proceed on the remaining premises, not to silently substitute a plan the brief never asked for. This is the counterpart to the blocked-sub-agent escalation: the same "stop and surface" posture, for the case where nothing is blocking you.
