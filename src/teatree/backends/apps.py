from django.apps import AppConfig


class BackendsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.backends"
    verbose_name = "TeaTree Backends"

    def ready(self) -> None:  # noqa: PLR6301
        from teatree.backends.backend_provider import install_backend_provider  # noqa: PLC0415
        from teatree.backends.slack_reactions import SlackReactionPublisher  # noqa: PLC0415
        from teatree.core.reaction_dispatch import register_reaction_publisher  # noqa: PLC0415

        register_reaction_publisher(SlackReactionPublisher())
        install_backend_provider()
