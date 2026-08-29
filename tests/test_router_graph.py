"""Structural tests for the Router/Value-Node graph scaffold (Phase 3).

These tests prove the specific claims design.md Section 7/Section 9 make
about the graph's *shape* -- reachability, the single declared back-edge,
and its counter-bound -- against the stubbed graph.  They do not test
business-logic quality (that is the Phase 8 scenario showcase); every node
here still returns a fixed placeholder value.
"""

from __future__ import annotations

import unittest
from unittest import mock

from starter.shopping_agent import graph
from starter.shopping_agent.state import (
    ConstraintMutation,
    IntentUpdate,
    NodeStateUpdate,
    SessionState,
    StateReducer,
)


class RouterGraphStructureTest(unittest.TestCase):
    def test_static_graph_is_dag_except_declared_back_edge(self) -> None:
        """Walk NODE_TRANSITIONS from Entry; the only allowed repeated node
        is the declared LoosenConstraints -> Search back-edge (design.md
        Section 7)."""

        def dfs(node: str, path: list[str]) -> None:
            if node == "Done":
                return
            if node in path:
                self.assertEqual(
                    (path[-1], node),
                    graph.DECLARED_BACK_EDGE,
                    f"unexpected repeated node via edge {(path[-1], node)}",
                )
                return
            for next_node in graph.NODE_TRANSITIONS.get(node, ()):
                dfs(next_node, [*path, node])

        dfs("Entry", [])

    def test_routers_cover_every_node_table_entry(self) -> None:
        """ROUTERS has one entry per design.md Section 4 row (18 total):
        the 14 predecessor-keyed entries (Entry + 12 Value-Node names +
        Render) plus the 4 human-readable router aliases."""

        expected = set(graph.NODE_TRANSITIONS) | {
            "IntentRouter2",
            "DistillTriggerRouter",
            "SlotCheckRouter",
            "CandidatePoolRouter",
        }
        self.assertEqual(set(graph.ROUTERS), expected)
        self.assertEqual(len(graph.ROUTERS), 18)

    def test_nodes_cover_every_value_node_and_terminal(self) -> None:
        expected = (set(graph.NODE_TRANSITIONS) - {"Entry"}) | {"Done"}
        self.assertEqual(set(graph.NODES), expected)

    def test_back_edge_guarded_by_retry_counter_cap(self) -> None:
        """Even if something bumped the counter twice, CandidatePoolRouter
        never re-selects LoosenConstraints once the cap (1) is reached."""

        session = SessionState(session_id="cap")
        reducer = StateReducer()
        reducer.apply(session, NodeStateUpdate(increment_search_retry=True))
        reducer.apply(session, NodeStateUpdate(increment_search_retry=True))
        self.assertEqual(session.search_retry_count, 1)  # capped, not 2

        gs = graph.GraphState(session=session, turn=1, top_k=10)
        node, _args = graph.ROUTERS["Search"](gs)
        self.assertNotEqual(node, "LoosenConstraints")


class _EmptySearchFixture:
    """Shared setup for the retry-bound / true-dead-end cases: a Search
    stub that always reports an empty candidate pool, wired in place of the
    real stub via ``NODES["Search"]``."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, gs: graph.GraphState, args: dict[str, object]) -> graph.GraphState:
        self.calls += 1
        StateReducer().apply(gs.session, NodeStateUpdate(candidates=()))
        return gs


class RetryBoundAndDeadEndTest(unittest.TestCase):
    """design.md Section 4.1 / Section 9: one retry, then either a targeted
    relax-conflict question or a true dead end -- never a third Search."""

    def test_retry_bound_then_relax_conflict_question(self) -> None:
        session = SessionState(session_id="retry")
        # Two soft constraints: Phase 4's real ``LoosenConstraints`` actually
        # drops the highest-priority one (design.md Section 4.1's fixed
        # order picks "material" before "color" -- see graph.py's
        # ``_LOOSEN_ATTRIBUTE_ORDER``), so a second constraint must remain
        # active for CandidatePoolRouter to have something left to offer
        # relaxing on the second empty Search.
        StateReducer().apply(
            session,
            IntentUpdate(
                mutations=(
                    ConstraintMutation(
                        action="upsert", attribute="color", value="red", hardness="soft"
                    ),
                    ConstraintMutation(
                        action="upsert", attribute="material", value="cotton", hardness="soft"
                    ),
                )
            ),
            turn=1,
        )
        self.assertEqual(len(session.active_constraints), 2)

        empty_search = _EmptySearchFixture()
        with mock.patch.dict(graph.NODES, {"Search": empty_search}):
            gs = graph.GraphState(session=session, turn=3, top_k=10)

            gs = graph.NODES["Search"](gs, {})  # first Search: empty
            node, args = graph.ROUTERS["Search"](gs)
            self.assertEqual(node, "LoosenConstraints")

            gs = graph.NODES[node](gs, args)  # bumps search_retry_count to 1
            self.assertEqual(len(session.active_constraints), 1)  # "material" dropped
            node, args = graph.ROUTERS[node](gs)
            self.assertEqual(node, "Search")  # the declared back-edge

            gs = graph.NODES[node](gs, args)  # second Search: still empty
            node, args = graph.ROUTERS["Search"](gs)
            self.assertEqual(node, "AskAttribute")
            self.assertEqual(args.get("mode"), "relax_conflict")
            self.assertEqual(args.get("attribute"), "color")  # the one constraint left

            gs = graph.NODES[node](gs, args)
            node, _args = graph.ROUTERS[node](gs)
            self.assertEqual(node, "Render")

        self.assertEqual(empty_search.calls, 2)  # never a third Search call
        self.assertEqual(session.search_retry_count, 1)

    def test_true_dead_end_reaches_no_match(self) -> None:
        session = SessionState(session_id="deadend")
        self.assertEqual(session.active_constraints, [])

        empty_search = _EmptySearchFixture()
        with mock.patch.dict(graph.NODES, {"Search": empty_search}):
            gs = graph.GraphState(session=session, turn=3, top_k=10)

            gs = graph.NODES["Search"](gs, {})  # first Search: empty
            node, args = graph.ROUTERS["Search"](gs)
            self.assertEqual(node, "LoosenConstraints")

            gs = graph.NODES[node](gs, args)
            node, args = graph.ROUTERS[node](gs)
            self.assertEqual(node, "Search")

            gs = graph.NODES[node](gs, args)  # second Search: still empty
            node, args = graph.ROUTERS["Search"](gs)
            self.assertEqual(node, "NoMatch")  # nothing left to relax

            gs = graph.NODES[node](gs, args)
            node, _args = graph.ROUTERS[node](gs)
            self.assertEqual(node, "Render")

        self.assertEqual(empty_search.calls, 2)
        self.assertEqual(session.search_retry_count, 1)


class RunGraphSmokeTest(unittest.TestCase):
    """One default-stub run proves the wiring is at least self-consistent;
    behavioral quality is not this test's job (Phase 8 scenario showcase)."""

    def test_default_stub_path_terminates(self) -> None:
        session = SessionState(session_id="smoke")
        gs = graph.run_graph(session, turn=1, top_k=10)
        self.assertIn("response", gs.scratch)


if __name__ == "__main__":
    unittest.main()
