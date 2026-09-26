from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

UPSTREAM_LEAF = ("core", "0092_taskattempt_taskattempt_recent_ended")
VENDORED_ROOT = ("core", "0084_red_mr_fix_attempt_review_findings")


class VendoredChainParentsTest(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.graph = MigrationLoader(None, ignore_no_migrations=True).graph

    def test_upstream_applied_history_has_no_vendored_parent(self) -> None:
        # A DB that applied upstream up to its leaf must stay consistent: none of that
        # leaf's ancestors may be a migration only the vendored chain ships.
        upstream_history = set(self.graph.forwards_plan(UPSTREAM_LEAF))
        vendored_descendants = set(self.graph.backwards_plan(VENDORED_ROOT))

        assert upstream_history.isdisjoint(vendored_descendants), sorted(upstream_history & vendored_descendants)

    def test_graph_has_a_single_core_leaf(self) -> None:
        assert len(self.graph.leaf_nodes("core")) == 1
