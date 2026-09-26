from django_typer.management import TyperCommand


class Command(TyperCommand):
    help = "Persist declared Notion pass-key routes without replacing database overrides."

    def handle(self) -> None:
        from teatree.config.credential_pass_key import (  # noqa: PLC0415 - command handler owns bootstrap imports
            PassKeySource,
            pass_key_setting,
        )
        from teatree.core.models.config_setting import ConfigSetting  # noqa: PLC0415 - app registry must be ready
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 - discovery loads registered overlays

        no_code_default = object()
        setting = pass_key_setting("notion_token")
        for overlay_name, overlay in get_all_overlays().items():
            resolution = overlay.config.resolve_pass_key("notion_token")
            if resolution.source is not PassKeySource.DECLARED_DEFAULT or not resolution.value:
                continue
            ConfigSetting.objects.seed(
                setting,
                resolution.value,
                code_default=no_code_default,
                scope=overlay_name,
            )
