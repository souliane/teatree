"""Control-DB row retention — the prune lanes and the seams they delegate to.

``prune`` is the orchestrator every lane resolves through; ``task_results`` is the
adapter onto ``django_tasks_db``'s own shipped prune, kept separate so the
orchestrator never imports a third-party table's library directly. Imported by
submodule path; no eager re-export (mock.patch targets name the defining submodule).
"""
