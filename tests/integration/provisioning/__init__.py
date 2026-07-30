"""Real-provisioning integration tests on a shared abstract base.

Where ``tests/teatree_core/test_provisioning_contract.py`` pins the
orchestration *logic* with docker/subprocess seams mocked, this package
provisions a *real* worktree, runs a *real* DB-touching test inside it,
starts the *real* server(s), and asserts they actually serve over HTTP
via :func:`teatree.core.worktree.readiness.run_probes`.

``_base.py`` holds the overlay-agnostic machinery as an ``abc.ABC``; each
``test_<target>.py`` subclasses it and fills in the per-target hooks. The
teatree self-test runs in normal CI (no docker, two concurrent worktrees).
The external-overlay test is env-gated (docker + a registered external
overlay whose repos resolve on disk + an opt-in flag) so default CI stays
green and the coverage gate is untouched.
"""
