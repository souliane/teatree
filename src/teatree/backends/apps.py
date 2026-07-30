from django.apps import AppConfig


class BackendsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.backends"
    verbose_name = "TeaTree Backends"

    def ready(self) -> None:  # noqa: PLR6301 — Django AppConfig.ready() hook; on the class by Django contract, uses no self
        from teatree.backends.attachment_fetchers import (  # noqa: PLC0415 — app-ready deferred import (app registry must load first)
            install_attachment_fetchers,
        )
        from teatree.backends.backend_provider import install_backend_provider  # noqa: PLC0415 — import cycle
        from teatree.backends.msteams.registration import (  # noqa: PLC0415 — app-ready deferred import (app registry must load first)
            install_presence_backends,
        )
        from teatree.backends.slack.reactions import SlackReactionPublisher  # noqa: PLC0415 — import cycle
        from teatree.core.reaction_dispatch import register_reaction_publisher  # noqa: PLC0415 — import cycle

        register_reaction_publisher(SlackReactionPublisher())
        install_backend_provider()
        install_attachment_fetchers()
        install_presence_backends()
