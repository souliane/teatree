"""A conversation's bookkeeping ticket: how long it lives (#5685).

The ticket a conversation lane mints is anchored on the synthetic loop umbrella, not on a forge
issue, so nothing findable ever tracks it and no plan or PR can be built on it. It hosts the
talk (answering, planning); the build phases belong to the forge ticket the request is
promoted into, which takes over the thread's binding. The refusal that keeps build phases off it
lives in ``plan_decision`` beside the other mint refusal.
"""

from datetime import timedelta

#: How long a thread with no live task stays a conversation; a later reply inside it finds its ticket.
CONVERSATION_QUIET_LIMIT = timedelta(days=7)
