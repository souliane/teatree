from teatree.core.models import Session


class SessionNotFoundError(Exception):
    pass


SessionNotFound = SessionNotFoundError


def get_active_session() -> Session:
    active = Session.objects.filter(ended_at__isnull=True).order_by("-started_at").first()
    if active is None:
        msg = "No active session found"
        raise SessionNotFound(msg)
    return active
