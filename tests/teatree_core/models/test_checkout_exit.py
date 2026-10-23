from teatree.core.models import Task
from teatree.core.models.checkout_exit import CheckoutExit, checkout_exit_scope, prepared_checkout_exit


def test_a_repeat_settle_is_reused_but_changed_exit_options_settle_afresh() -> None:
    options: list[tuple[bool, bool]] = []

    def settle(*, attest: bool, wait: bool) -> CheckoutExit:
        options.append((attest, wait))
        return CheckoutExit(release=True, attested_state="digest" if attest else None)

    task = Task(pk=7)
    with checkout_exit_scope(lambda _task, attest, wait: settle(attest=attest, wait=wait)):
        prepared_checkout_exit(task)
        prepared_checkout_exit(task)
        prepared_checkout_exit(task, wait=False)
        settled = prepared_checkout_exit(task, attest=False, wait=False)

    assert options == [(True, True), (True, False), (False, False)]
    assert settled is not None
    assert settled.attested_state is None
