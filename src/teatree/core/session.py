from teatree.core.models import Session
from teatree.core.session_identity import current_session_id


class SessionNotFound(LookupError):  # noqa: N818
    pass


def get_active_session() -> Session:
    agent_id = current_session_id()
    session = Session.objects.filter(agent_id=agent_id, ended_at__isnull=True).order_by("-pk").first()
    if session is None:
        raise SessionNotFound
    return session
