"""Roster projection tests.

Tests for roster_view (reveal-gated projection) and disposition_band.
stdlib unittest; zero LLM calls; zero live services.
Runs in the test container (network: none).

Test suites:
  R1 - whitelist excludes witnessed/reveal_state/bound_status
  R2 - disposition_band boundaries (same thresholds as _disposition_phrase)
  R3 - empty state -> []
  R4 - sort: last_seen_turn desc, then name asc
"""

import unittest

# ---------------------------------------------------------------------------
# Minimal fake state
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(self, npcs=None, location="market"):
        self.npcs = npcs or {}
        self.location = location

    def to_dict(self):
        return {}


def _npc_dict(**kw):
    base = {
        "npc_id": "test-npc",
        "name": "Tester",
        "role": "merchant",
        "persona": "A jovial seller.",
        "location": "market",
        "witnessed": ["turn 1: player arrived"],
        "disposition": 0,
        "first_seen_turn": 1,
        "last_seen_turn": 2,
        "color_hue": 35,
        "reveal_state": {"saw_what": ["secret"], "believes_what": [], "will_do_what": []},
        "bound_status": "wrists tied",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# R1 - whitelist
# ---------------------------------------------------------------------------


class TestWhitelist(unittest.TestCase):
    def setUp(self):
        self.npc = _npc_dict(npc_id="marta-healer", name="Marta", disposition=10, last_seen_turn=3)
        self.state = _FakeState(npcs={"marta-healer": self.npc})

    def _roster(self):
        from app.game.npcs import roster_view

        return roster_view(self.state)

    def test_whitelist_includes_required_fields(self):
        rows = self._roster()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for field in (
            "npc_id",
            "name",
            "role",
            "persona",
            "disposition",
            "disposition_band",
            "first_seen_turn",
            "last_seen_turn",
            "color_hue",
        ):
            self.assertIn(field, r, f"missing whitelisted field: {field}")

    def test_whitelist_excludes_witnessed(self):
        rows = self._roster()
        self.assertNotIn("witnessed", rows[0])

    def test_whitelist_excludes_reveal_state(self):
        rows = self._roster()
        self.assertNotIn("reveal_state", rows[0])

    def test_whitelist_excludes_bound_status(self):
        rows = self._roster()
        self.assertNotIn("bound_status", rows[0])

    def test_whitelist_excludes_location(self):
        rows = self._roster()
        self.assertNotIn("location", rows[0])

    def test_disposition_band_in_result(self):
        rows = self._roster()
        # disposition=10 -> neutral
        self.assertEqual(rows[0]["disposition_band"], "neutral")


# ---------------------------------------------------------------------------
# R2 - disposition_band boundaries
# ---------------------------------------------------------------------------


class TestDispositionBand(unittest.TestCase):
    def _band(self, d):
        from app.game.npcs import disposition_band

        return disposition_band(d)

    def test_hostile_boundary_exactly(self):
        self.assertEqual(self._band(-60), "hostile")

    def test_hostile_lower(self):
        self.assertEqual(self._band(-100), "hostile")

    def test_hostile_minus61(self):
        self.assertEqual(self._band(-61), "hostile")

    def test_wary_boundary_exactly(self):
        self.assertEqual(self._band(-25), "wary")

    def test_wary_range(self):
        self.assertEqual(self._band(-59), "wary")

    def test_neutral_at_minus24(self):
        self.assertEqual(self._band(-24), "neutral")

    def test_neutral_at_zero(self):
        self.assertEqual(self._band(0), "neutral")

    def test_neutral_at_24(self):
        self.assertEqual(self._band(24), "neutral")

    def test_cordial_at_25(self):
        self.assertEqual(self._band(25), "cordial")

    def test_cordial_at_59(self):
        self.assertEqual(self._band(59), "cordial")

    def test_warm_at_60(self):
        self.assertEqual(self._band(60), "warm")

    def test_warm_at_100(self):
        self.assertEqual(self._band(100), "warm")

    def test_warm_is_not_warm_trusting(self):
        # disposition_band returns "warm", not "warm / trusting"
        # (which is what _disposition_phrase returns)
        self.assertNotEqual(self._band(60), "warm / trusting")


# ---------------------------------------------------------------------------
# R3 - empty state
# ---------------------------------------------------------------------------


class TestEmptyState(unittest.TestCase):
    def test_empty_npcs_returns_empty_list(self):
        from app.game.npcs import roster_view

        state = _FakeState(npcs={})
        rows = roster_view(state)
        self.assertEqual(rows, [])

    def test_none_npcs_returns_empty_list(self):
        from app.game.npcs import roster_view

        state = _FakeState(npcs=None)
        state.npcs = None
        rows = roster_view(state)
        self.assertEqual(rows, [])

    def test_never_raises(self):
        from app.game.npcs import roster_view

        # Pathological state - should not raise
        class BadState:
            npcs = "not a dict"
            location = ""

            def to_dict(self):
                return {}

        rows = roster_view(BadState())
        self.assertIsInstance(rows, list)


# ---------------------------------------------------------------------------
# R4 - sort order
# ---------------------------------------------------------------------------


class TestSortOrder(unittest.TestCase):
    def test_sort_last_seen_desc_then_name_asc(self):
        from app.game.npcs import roster_view

        state = _FakeState(
            npcs={
                "alice-npc": _npc_dict(npc_id="alice-npc", name="Alice", last_seen_turn=5),
                "bob-npc": _npc_dict(npc_id="bob-npc", name="Bob", last_seen_turn=10),
                "carol-npc": _npc_dict(npc_id="carol-npc", name="Carol", last_seen_turn=5),
            }
        )
        rows = roster_view(state)
        names = [r["name"] for r in rows]
        # Bob last_seen_turn=10 is first (highest).
        # Alice and Carol both last_seen_turn=5 -> alphabetical.
        self.assertEqual(names, ["Bob", "Alice", "Carol"])


if __name__ == "__main__":
    unittest.main()
