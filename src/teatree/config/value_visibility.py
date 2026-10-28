"""Cold-safe value masking policy shared by display and audit storage."""

from teatree.config.extra_headers import carries_unlisted_header
from teatree.config.schema import Category, TeatreeSettingsSchema, setting_meta
from teatree.config.secret_settings import PERSONAL_IDENTIFIERS, SECRET_SETTINGS, is_credential_reference

MASKED = "***"


def is_secret(setting: str) -> bool:
    if setting in SECRET_SETTINGS or setting in PERSONAL_IDENTIFIERS or is_credential_reference(setting):
        return True
    if setting not in TeatreeSettingsSchema.model_fields:
        return False
    return setting_meta(setting).category is Category.SECRET


def withholds_value(setting: str, value: object) -> bool:
    return is_secret(setting) or carries_unlisted_header(setting, value)
