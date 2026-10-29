"""Declared trigger class for every loop scanner."""

CONTENT_POLLERS: frozenset[str] = frozenset(
    {
        "BacklogSweepScanner",
        "ClaudeSelfPrReviewScanner",
        "CodexReviewScanner",
        "IssueIntakeScanner",
        "MyPrsScanner",
        "TriageAssessorScanner",
    }
)

STATE_POLLERS: frozenset[str] = frozenset(
    {
        "GitLabApprovalsScanner",
        "IssueDispositionScanner",
        "MrConflictScanner",
        "MrTriageScanner",
        "PendingPrDrainScanner",
        "PrApprovalScanner",
        "PrSweepScanner",
        "ReviewNagScanner",
        "ReviewRequestMergeReactScanner",
        "ReviewRequestResumeScanner",
        "ReviewRequestSendScanner",
        "ReviewedPrHeadScanner",
        "ReviewerPrsScanner",
        "UnauditedMergeScanner",
    }
)

NOT_FORGE: frozenset[str] = frozenset(
    {
        "ActiveTicketsScanner",
        "ArchitecturalReviewScanner",
        "AskUserQuestionReplyScanner",
        "BoardReconcileScanner",
        "DbBackupScanner",
        "DeferredQuestionPosterScanner",
        "EvalLocalScanner",
        "IdleStackReaperScanner",
        "IncomingEventsScanner",
        "LocalStackQueueDrainerScanner",
        "OutboundAuditScanner",
        "OwnerActionScanner",
        "PendingTasksScanner",
        "ProvisionSmokeScanner",
        "QuestionBacklogNagScanner",
        "RedCardScanner",
        "ResourcePressureScanner",
        "ReviewDoneAckScanner",
        "ScanningNewsScanner",
        "SelfUpdateScanner",
        "SlackMentionsScanner",
        "SlackReviewIntentScanner",
        "StaleTicketsScanner",
        "TaskSweepScanner",
        "TicketCompletionScanner",
        "TicketDispositionScanner",
        "UndeliveredNotifyScanner",
        "WaitingDigestScanner",
        "WorkStateScanner",
    }
)
