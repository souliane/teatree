from django.core.exceptions import ImproperlyConfigured

from teatree.config.credential_pass_key import CORE_CREDENTIALS, credential_of_setting
from teatree.core.overlay_loader import get_all_overlays, get_overlay


def known_credentials() -> frozenset[str]:
    return CORE_CREDENTIALS.union(*(overlay.config.declared_credentials() for overlay in get_all_overlays().values()))


def known_pass_key_credential(setting: str) -> str | None:
    credential = credential_of_setting(setting)
    if credential is None:
        return None
    if credential in CORE_CREDENTIALS:
        return credential
    declared = any(credential in overlay.config.declared_credentials() for overlay in get_all_overlays().values())
    return credential if declared else None


def overlay_pass_key(credential: str, overlay_name: str | None = None) -> str:
    try:
        overlay = get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return ""
    return overlay.config.secret_pass_key(credential)
