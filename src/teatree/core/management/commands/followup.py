from django_typer.management import TyperCommand, command

from teatree.core.models import Task, Ticket


class Command(TyperCommand):
    @command()
    def refresh(self) -> dict[str, int]:
        return {
            "tickets": Ticket.objects.count(),
            "tasks": Task.objects.count(),
            "open_tasks": Task.objects.exclude(status=Task.Status.COMPLETED).count(),
        }

    @command()
    def sync(self) -> dict[str, int | list[str]]:
        from teatree.core.sync import sync_followup  # noqa: PLC0415

        result = sync_followup()
        return {
            "mrs_found": result.mrs_found,
            "tickets_created": result.tickets_created,
            "tickets_updated": result.tickets_updated,
            "errors": result.errors,
        }

    @command()
    def remind(self) -> list[int]:
        return list(
            Task.objects.filter(
                execution_target=Task.ExecutionTarget.INTERACTIVE,
                status=Task.Status.PENDING,
            )
            .order_by("pk")
            .values_list("id", flat=True),
        )
