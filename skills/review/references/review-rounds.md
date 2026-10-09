# Review rounds on one MR

The rules behind `/t3:review` § "Receiving Code Review" for a multi-round review of one MR.

- **Fix the defect class each round.** Grep every instance and consumer, then report a consumer table with each disposition; fixing only cited lines does not close the finding.
- **Carry review context forward.** Give delta reviewers the prior rounds' findings and decisions; verify the new delta without reopening settled calls unless new evidence invalidates them.
- **Three review rounds per MR is the cap.** A round-three BLOCK ends patching on that MR; what follows is an explicit decision to split its scope or redesign the approach.
