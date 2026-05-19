"""Locks the approval helpers into :mod:`teatree.cli.review_approvals`.

Created alongside the #1056 split of ``review.py`` (502 LOC, at the
``scripts/hooks/check_module_health.py`` 500-LOC ceiling). The approve
/ unapprove / review-precondition cluster was moved into its own
sibling module so the parent module-health gate stays satisfied as the
review surface grows.

This test pins the **module location** so a future "tidy-up" can't
quietly move the helpers back to ``review.py`` and re-violate the LOC
budget. The behavioural contract continues to live in
``test_review_approve_gate.py`` /
``test_review_on_behalf_gate.py`` / ``test_cli_review.py``; those
files still import ``ReviewService`` from ``teatree.cli.review`` and
prove every method (approve, unapprove) behaves identically after the
move — so this file deliberately stays narrow.
"""

import importlib


class TestReviewApprovalsModule:
    """The approval cluster has its own importable module."""

    def test_module_imports(self) -> None:
        """``teatree.cli.review_approvals`` is importable."""
        importlib.import_module("teatree.cli.review_approvals")

    def test_module_exposes_register(self) -> None:
        """The module exposes a ``register(review_app)`` wiring entrypoint.

        Mirrors :mod:`teatree.cli.review_drafts` /
        :mod:`teatree.cli.review_on_behalf`: each split-out module
        registers its own typer commands on the shared ``review_app``,
        invoked from :mod:`teatree.cli.review` at import time.
        """
        module = importlib.import_module("teatree.cli.review_approvals")
        assert callable(module.register)

    def test_approve_and_unapprove_callables_exist(self) -> None:
        """Free-function entry points for the approval helpers exist.

        The ``ReviewService.approve`` / ``unapprove`` methods on
        :class:`teatree.cli.review.ReviewService` still exist (the
        public service surface is unchanged), but the implementation
        body lives here as a free function the service method
        delegates to. Pins the delegation target so it stays in this
        module.
        """
        module = importlib.import_module("teatree.cli.review_approvals")
        assert callable(module.approve)
        assert callable(module.unapprove)
