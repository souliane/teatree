from django.conf import settings


def get_terminal_mode() -> str:
    return getattr(settings, "TEATREE_TERMINAL_MODE", "ttyd")
