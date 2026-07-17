---
name: triaging-issues
description: Review and act on the needs-triage assessor's queued recommendations — list PENDING PendingTriageRecommendation rows, approve or reject each, and on approval run `gh issue close/edit/comment` then stamp the row. Use when user says "triage issues", "triaging issues", "review triage recommendations", "assess needs-triage", or catches up on the triage-assessor batch.
compatibility: macOS/Linux, git, gh CLI.
requires:
  - rules
  - platforms
eval_exempt: interactive per-row approval/action flow over `gh` + a Django ORM ask-gate; the assessor agent's judgment is graded elsewhere and the mechanical persistence/dedup is covered by tests/teatree_core/test_pending_triage_recommendation.py and tests/teatree_agents/test_attempt_recorder.py
metadata:
  version: 0.0.1
  subagent_safe: false
---

# t3:triaging-issues — Approve & Act on Triage Recommendations

The `triage_assessor` loop discovers OPEN `needs-triage` issues and has a
shell-denied agent assess each, returning a keep/close/needs-info verdict.
Each assessment is persisted as a `PENDING` `PendingTriageRecommendation`
row, and one `DeferredQuestion` DMs you the batch. **Nothing has been acted
on.** This skill is the human-in-the-loop approval surface: you review each
row and, only on your approval, act via `gh`.

## Non-Negotiables

1. **Nothing acts without your per-row approval.** The assessor never closes,
   comments on, or relabels an issue. Only an approval here runs `gh`.
2. **`close` is conservative.** If a recommendation to close looks wrong,
   reject it — the `needs-triage` label stays and a human keeps it.
3. **No AI signature** on issue comments or edits (per `t3:rules`).
4. **Stamp every decision.** After acting (or rejecting), record the outcome
   on the row so a re-assessment never re-queues the issue (dedup is by issue
   URL, and a decided row still blocks re-queue).

## Command Reference

```bash
# List the PENDING recommendations awaiting your decision.
uv run python manage.py shell -c "from teatree.core.models import PendingTriageRecommendation as P; [print(r.pk, r.verdict, r.issue_url, '::', r.rationale) for r in P.objects.filter(status=P.Status.PENDING)]"

# --- On APPROVAL, act per verdict, then stamp the row ---

# close: close the issue with an audit-trail comment citing the rationale.
gh --repo souliane/teatree issue close <number> --comment "<rationale> (triage-assessor, approved)"

# close-as-duplicate: cross-link the superseding issue.
gh --repo souliane/teatree issue close <number> --comment "Duplicate of <duplicate_of> — closing (triage-assessor, approved)"

# needs_info: relabel off needs-triage and ask for detail (never auto-close).
gh --repo souliane/teatree issue edit <number> --remove-label needs-triage --add-label needs-info
gh --repo souliane/teatree issue comment <number> --body "<what's missing before this can be worked>"

# keep: usually a no-op (leave open); optionally drop needs-triage if it is now
# clearly actionable and you want it in the normal queue.
gh --repo souliane/teatree issue edit <number> --remove-label needs-triage

# Stamp the approval with what you did (audit trail).
uv run python manage.py shell -c "from teatree.core.models import PendingTriageRecommendation as P; r=P.objects.get(pk=<id>); r.approve(action_taken='closed #<number> as <verdict>')"

# --- On REJECTION, take no action on the issue — just stamp the row ---
uv run python manage.py shell -c "from teatree.core.models import PendingTriageRecommendation as P; P.objects.get(pk=<id>).reject()"
```

## Workflow

### 1. List the PENDING batch

Run the list command above. For each row note: `pk`, `verdict`,
`issue_url`, `suggested_labels`, `priority`, `duplicate_of`, and the
`rationale`.

### 2. Decide per row

Present each recommendation to the user one line at a time (verdict + why +
URL) and get an explicit approve/reject. Bias toward rejecting a `close` you
are unsure about — a wrongly-closed issue is worse than one left triaged.

### 3. Act on approval

Run the matching `gh` command for the verdict (close / needs_info relabel /
keep). Every `close` gets an audit-trail comment citing the rationale so the
close is explainable. A `close` with a `duplicate_of` cross-links the
superseding issue.

### 4. Stamp the decision

Call `row.approve(action_taken=...)` (recording exactly what you did) on
approval, or `row.reject()` on rejection. This closes the ask-gate loop and
prevents the next assessor tick from re-queuing the same issue.

## Rules

- Nothing acts without per-row user approval.
- `close` is conservative — reject an uncertain close.
- Every close carries an audit-trail comment citing the rationale.
- Stamp every decision (`approve`/`reject`) so the issue is not re-queued.
- No AI signature on comments or edits.

## Related skills

- `t3:sweeping-tickets` — the broader evidence-gated consolidation/triage
  flow; this skill is the per-issue approval surface for the automated
  assessor's recommendations.
- `t3:availability` — the `DeferredQuestion` this skill answers is queued by
  the away-mode question surface.

---

*If this skill was truncated during context compression, re-read it from disk before acting on any recommendation.*
