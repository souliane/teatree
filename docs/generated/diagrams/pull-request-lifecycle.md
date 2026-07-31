# PullRequest lifecycle

<!-- BEGIN GENERATED: pull-request-fsm -->
```mermaid
stateDiagram-v2
    [*] --> open
    open --> review_requested : request_review
    open --> merged : mark_merged
    open --> closed : mark_closed
    review_requested --> approved : approve
    review_requested --> merged : mark_merged
    review_requested --> closed : mark_closed
    approved --> merged : mark_merged
    approved --> closed : mark_closed
```
<!-- END GENERATED: pull-request-fsm -->
