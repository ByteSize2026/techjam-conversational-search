"""Regression test for showcase b (artifacts/full_live_test/
final_check_20_20260830_010957, public_0002/public_0013): ``ClarificationPolicy``
re-asked about an attribute the customer had just supplied via an override,
because eligibility only checked ``asked_set`` (what the policy itself asked)
and ``no_preference`` (an explicit decline) -- never whether the attribute
already has a known value from any other source.
"""

from __future__ import annotations

import unittest

from starter.shopping_agent.policy import ClarificationPolicy
from starter.shopping_agent.state import Constraint, SessionState


class ClarificationPolicyKnownAttributeTest(unittest.TestCase):
    def test_does_not_reask_attribute_already_set_via_override(self) -> None:
        """Mirrors public_0002 live: an override commits material=leather as
        a hard constraint without ``choose_attribute`` ever having asked for
        it -- it must not pick ``material`` again."""

        state = SessionState(
            session_id="s1",
            constraints=[
                Constraint(
                    attribute="material",
                    value="leather",
                    polarity="require",
                    hardness="hard",
                    confidence=0.85,
                )
            ],
        )
        candidates = [
            {"title": "Leather belt, black, size M", "store": "Acme"},
        ]

        chosen = ClarificationPolicy().choose_attribute(state, candidates, turn=3)

        self.assertNotEqual(chosen, "material")

    def test_control_asks_about_material_when_it_is_actually_unknown(self) -> None:
        """Control for the above: with no active constraint at all, the same
        candidate evidence should still pick ``material`` -- the fix must not
        turn off asking altogether, only skip attributes already known."""

        state = SessionState(session_id="s2")
        candidates = [
            {"title": "Leather belt, black, size M", "store": "Acme"},
        ]

        chosen = ClarificationPolicy().choose_attribute(state, candidates, turn=3)

        self.assertEqual(chosen, "material")


if __name__ == "__main__":
    unittest.main()
