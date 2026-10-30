from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.core.models import OperatorIdentity, Task
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from tests.teatree_loop.slack_answer.test_inbound_orchestration import RecordingBackend, _reader, _resolver, _row, _work


class TestRegisteredOperatorSafety(TestCase):
    def test_registered_operator_cannot_set_priority_as_work(self) -> None:
        user = get_user_model().objects.create_superuser(username="operator", email="", password=None)
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-operator")
        _row("mark #12 urgent", ts="1.0", user_id="U-operator")

        report = run_slack_answer_cycle(
            messaging_resolver=_resolver(RecordingBackend()),
            reader=_reader(_work("mark ticket 12 urgent")),
        )

        assert report.dispatched == 0
        assert not Task.objects.exists()
