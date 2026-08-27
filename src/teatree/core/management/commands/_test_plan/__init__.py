"""Test-plan helpers — clustered by the ``e2e`` command.

Private helpers backing ``t3 <overlay> e2e write-test-plan``: where the plan
file lives (:mod:`.file_store`), the write orchestration (:mod:`.write`), the
render layer (:mod:`.render`), the scenario renderer (:mod:`.scenario`), and the
workflow-template renderers (:mod:`.workflow_templates`). The MR/PR-comment
poster (:mod:`.mr_post`) is a separate surface. Imported by submodule path; no
eager re-export.
"""
