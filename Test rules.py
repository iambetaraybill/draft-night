"""
Tests for the money and squad rules.

These matter more than anything else in the project. A cosmetic bug in the
commentary is a laugh; a bug here means someone's draft night ends in an
argument about whether the app cheated.

Run: python -m unittest discover -s tests -v
"""

import unittest

from draftnight.rules import (
    DEFAULT_FORMATION,
    Formation,
    Manager,
    Player,
    Rules,
)


def mk_player(pid="p1", name="Test Player", pos="FWD", rating=80):
    return Player(pid=pid, name=name, pos=pos, rating=rating)


def mk_manager(budget=100, is_ai=False):
    return Manager(mid="m1", name="Tester", budget=budget, is_ai=is_ai)


class TestSquadShape(unittest.TestCase):
    def test_default_formation_is_eight(self):
        self.assertEqual(DEFAULT_FORMATION.size, 8)

    def test_needs_starts_at_formation(self):
        r = Rules()
        m = mk_manager()
        self.assertEqual(r.needs(m), {"GK": 1, "DEF": 3, "MID": 2, "FWD": 2})
        self.assertEqual(r.slots_left(m), 8)

    def test_needs_drops_after_signing(self):
        r = Rules()
        m = mk_manager()
        r.award(m, mk_player(pos="GK"), 5)
        self.assertEqual(r.needs(m)["GK"], 0)
        self.assertFalse(r.needs_position(m, "GK"))
        self.assertEqual(r.slots_left(m), 7)

    def test_squad_full_blocks_everything(self):
        r = Rules(Formation({"FWD": 1}))
        m = mk_manager()
        r.award(m, mk_player(pos="FWD"), 1)
        self.assertTrue(r.squad_full(m))
        ok, why = r.check_bid(m, mk_player(pid="p2", pos="FWD"), 1)
        self.assertFalse(ok)
        self.assertIn("full", why.lower())


class TestBudgetReservation(unittest.TestCase):
    """The core protection: never spend money you need for empty slots."""

    def test_max_bid_reserves_for_remaining_slots(self):
        # 8 slots, 100 budget, min bid 1 -> can spend 93 on the first player
        r = Rules(min_bid=1)
        m = mk_manager(budget=100)
        self.assertEqual(r.max_bid(m), 93)

    def test_max_bid_scales_with_min_bid(self):
        # 8 slots at min 5 -> must keep 35 back for the other 7
        r = Rules(min_bid=5)
        m = mk_manager(budget=100)
        self.assertEqual(r.max_bid(m), 65)

    def test_last_slot_can_use_everything(self):
        r = Rules(Formation({"FWD": 2}), min_bid=1)
        m = mk_manager(budget=50)
        r.award(m, mk_player(pos="FWD"), 10)
        self.assertEqual(r.slots_left(m), 1)
        self.assertEqual(r.max_bid(m), 40)

    def test_full_squad_has_no_max_bid(self):
        r = Rules(Formation({"FWD": 1}))
        m = mk_manager(budget=100)
        r.award(m, mk_player(pos="FWD"), 1)
        self.assertEqual(r.max_bid(m), 0)

    def test_cannot_be_left_unable_to_fill_squad(self):
        """Spend the cap every time and the squad still completes."""
        r = Rules(min_bid=2)
        m = mk_manager(budget=200)
        order = ["GK", "DEF", "DEF", "DEF", "MID", "MID", "FWD", "FWD"]
        for i, pos in enumerate(order):
            cap = r.max_bid(m)
            self.assertGreaterEqual(cap, r.min_bid, f"broke at slot {i}")
            r.award(m, mk_player(pid=f"p{i}", pos=pos), cap)
        self.assertTrue(r.squad_full(m))
        self.assertGreaterEqual(r.budget_left(m), 0)

    def test_overspend_is_rejected_with_a_useful_reason(self):
        r = Rules(min_bid=1)
        m = mk_manager(budget=100)
        ok, why = r.check_bid(m, mk_player(), 94)
        self.assertFalse(ok)
        self.assertIn("93", why)

    def test_award_raises_on_illegal_price(self):
        r = Rules(min_bid=1)
        m = mk_manager(budget=100)
        with self.assertRaises(ValueError):
            r.award(m, mk_player(), 94)


class TestBidIncrements(unittest.TestCase):
    def test_first_bid_is_min_bid(self):
        r = Rules(min_bid=3)
        self.assertEqual(r.next_bid(None), 3)

    def test_next_bid_adds_increment(self):
        r = Rules(increment=5)
        self.assertEqual(r.next_bid(20), 25)

    def test_bid_below_increment_rejected(self):
        r = Rules(increment=5)
        m = mk_manager()
        ok, why = r.check_bid(m, mk_player(), 22, current_bid=20)
        self.assertFalse(ok)
        self.assertIn("25", why)

    def test_cannot_outbid_yourself(self):
        r = Rules()
        m = mk_manager()
        ok, why = r.check_bid(m, mk_player(), 30, current_bid=20, current_bidder="m1")
        self.assertFalse(ok)
        self.assertIn("top bid", why.lower())


class TestPositionNeed(unittest.TestCase):
    def test_wrong_position_rejected_and_lists_what_is_open(self):
        r = Rules(Formation({"GK": 1, "FWD": 1}))
        m = mk_manager()
        r.award(m, mk_player(pos="GK"), 5)
        ok, why = r.check_bid(m, mk_player(pid="p9", pos="GK"), 6)
        self.assertFalse(ok)
        self.assertIn("FWD", why)

    def test_eligible_buyers_excludes_full_and_wrong_position(self):
        r = Rules(Formation({"GK": 1, "FWD": 1}))
        a = Manager(mid="a", name="A", budget=50)
        b = Manager(mid="b", name="B", budget=50)
        r.award(b, mk_player(pos="GK"), 5)
        keeper = mk_player(pid="k", pos="GK")
        self.assertEqual([m.mid for m in r.eligible_buyers([a, b], keeper)], ["a"])

    def test_broke_manager_is_not_an_eligible_buyer(self):
        r = Rules(Formation({"FWD": 2}), min_bid=1)
        poor = Manager(mid="poor", name="Poor", budget=2)
        r.award(poor, mk_player(pos="FWD"), 1)
        # 1 left, 1 slot -> can still bid the minimum
        self.assertTrue(r.can_bid_at_all(poor, mk_player(pid="x", pos="FWD")))
        # but not against an existing bid of 1
        self.assertFalse(
            r.can_bid_at_all(poor, mk_player(pid="x", pos="FWD"), current_bid=1)
        )


class TestValidation(unittest.TestCase):
    def test_unknown_position_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            Player(pid="x", name="Nobody", pos="SWEEPER", rating=70)

    def test_bad_config_rejected(self):
        with self.assertRaises(ValueError):
            Rules(min_bid=0)
        with self.assertRaises(ValueError):
            Rules(increment=0)


if __name__ == "__main__":
    unittest.main()
