"""
Tests for the proxy bidder.

The model is treated as an untrusted source of suggestions. These tests pin
down the thing that makes that safe: whatever number comes back, the bid that
reaches the auction is legal. A real model is not needed to prove it, so the
HTTP call is stubbed and the clamping is checked directly.

Run: python -m unittest discover -s tests -t . -v
"""

import unittest
from unittest import mock

from draftnight import agents
from draftnight.rules import Formation, Manager, Player, Rules


def mk(budget=100, is_ai=True, strategy="Get a keeper early."):
    return Manager(mid="a1", name="Sam", budget=budget, is_ai=is_ai, strategy=strategy)


KEEPER = Player(pid="k1", name="Niko Jovic", pos="GK", rating=91, club="Harbourside")


class TestClamping(unittest.TestCase):
    def test_absurd_model_bid_is_clamped_to_the_cap(self):
        rules = Rules(min_bid=1)  # 8 slots, so cap is budget - 7
        m = mk(budget=100)
        reply = {"want": True, "max_bid": 99999, "reason": "Best keeper available."}

        with mock.patch.object(agents, "_call_ollama", return_value=reply):
            plan = agents.plan_for_lot(m, KEEPER, rules)

        self.assertEqual(plan.max_bid, rules.max_bid(m))
        self.assertEqual(plan.max_bid, 93)
        self.assertEqual(plan.source, "llm")

    def test_clamping_is_counted(self):
        rules = Rules(min_bid=1)
        before = agents.STATS.snapshot()["clamped"]
        with mock.patch.object(
            agents,
            "_call_ollama",
            return_value={"want": True, "max_bid": 5000, "reason": "x"},
        ):
            agents.plan_for_lot(mk(), KEEPER, rules)
        self.assertEqual(agents.STATS.snapshot()["clamped"], before + 1)

    def test_negative_model_bid_becomes_zero(self):
        rules = Rules()
        with mock.patch.object(
            agents,
            "_call_ollama",
            return_value={"want": True, "max_bid": -40, "reason": "no thanks"},
        ):
            plan = agents.plan_for_lot(mk(), KEEPER, rules)
        self.assertEqual(plan.max_bid, 0)
        self.assertFalse(plan.want)

    def test_junk_reply_does_not_crash(self):
        """A model that ignores the schema entirely falls back, not over."""
        rules = Rules()
        with mock.patch.object(
            agents, "_call_ollama", return_value={"maybe": "who knows"}
        ):
            plan = agents.plan_for_lot(mk(), KEEPER, rules)
        self.assertFalse(plan.want)
        self.assertEqual(plan.max_bid, 0)

    def test_overlong_reason_is_trimmed(self):
        rules = Rules()
        with mock.patch.object(
            agents,
            "_call_ollama",
            return_value={"want": True, "max_bid": 10, "reason": "y" * 900},
        ):
            plan = agents.plan_for_lot(mk(), KEEPER, rules)
        self.assertLessEqual(len(plan.reason), 120)


class TestFallback(unittest.TestCase):
    def test_model_failure_falls_back_to_heuristic(self):
        rules = Rules()
        with mock.patch.object(
            agents, "_call_ollama", side_effect=ConnectionError("ollama is not running")
        ):
            plan = agents.plan_for_lot(mk(), KEEPER, rules)
        self.assertEqual(plan.source, "heuristic")
        self.assertLessEqual(plan.max_bid, rules.max_bid(mk()))

    def test_heuristic_respects_the_cap(self):
        rules = Rules(Formation({"GK": 1, "FWD": 1}), min_bid=1)
        m = mk(budget=20)
        plan = agents.heuristic_plan(m, KEEPER, rules, rules.max_bid(m))
        self.assertLessEqual(plan.max_bid, rules.max_bid(m))

    def test_no_slot_means_no_plan_and_no_model_call(self):
        rules = Rules(Formation({"FWD": 1}))
        m = mk()
        with mock.patch.object(agents, "_call_ollama") as called:
            plan = agents.plan_for_lot(m, KEEPER, rules)
        called.assert_not_called()
        self.assertEqual(plan.source, "skipped")
        self.assertFalse(plan.want)

    def test_broke_manager_never_reaches_the_model(self):
        rules = Rules(Formation({"GK": 1, "FWD": 1}), min_bid=5)
        m = mk(budget=5)  # cap is 0 with a slot still to reserve for
        with mock.patch.object(agents, "_call_ollama") as called:
            plan = agents.plan_for_lot(m, KEEPER, rules)
        called.assert_not_called()
        self.assertEqual(plan.source, "skipped")


class TestTactics(unittest.TestCase):
    def test_no_bid_when_plan_declines(self):
        rules = Rules()
        plan = agents.Plan(want=False, max_bid=50, reason="")
        self.assertIsNone(agents.tactical_bid(plan, 10, rules))

    def test_no_bid_above_the_plan_ceiling(self):
        rules = Rules(increment=1)
        plan = agents.Plan(want=True, max_bid=20, reason="")
        self.assertIsNone(agents.tactical_bid(plan, 20, rules))
        self.assertIsNone(agents.tactical_bid(plan, 25, rules))

    def test_bids_the_minimum_raise_or_a_jump_but_never_over(self):
        rules = Rules(increment=1)
        plan = agents.Plan(want=True, max_bid=40, reason="")
        for _ in range(200):
            bid = agents.tactical_bid(plan, 10, rules)
            self.assertIsNotNone(bid)
            self.assertGreaterEqual(bid, 11)
            self.assertLessEqual(bid, 40)

    def test_opens_at_or_above_min_bid_on_a_fresh_lot(self):
        rules = Rules(min_bid=3)
        plan = agents.Plan(want=True, max_bid=30, reason="")
        for _ in range(100):
            bid = agents.tactical_bid(plan, None, rules)
            self.assertGreaterEqual(bid, 3)
            self.assertLessEqual(bid, 30)

    def test_opening_bid_is_exactly_min_when_there_is_no_headroom(self):
        rules = Rules(min_bid=3, increment=1)
        plan = agents.Plan(want=True, max_bid=4, reason="")
        self.assertEqual(agents.tactical_bid(plan, None, rules), 3)


class TestPromptAssembly(unittest.TestCase):
    def test_strategy_and_cap_reach_the_prompt(self):
        """The friend's own words have to actually arrive at the model."""
        rules = Rules()
        m = mk(budget=120, strategy="Never spend more than 40% on one player.")
        prompt = agents._build_prompt(m, KEEPER, rules, rules.max_bid(m), "")
        self.assertIn("Never spend more than 40%", prompt)
        self.assertIn(str(rules.max_bid(m)), prompt)
        self.assertIn("Niko Jovic", prompt)
        self.assertIn("1 GK", prompt)

    def test_missing_strategy_is_handled(self):
        rules = Rules()
        m = mk(strategy="")
        prompt = agents._build_prompt(m, KEEPER, rules, 50, "")
        self.assertIn("balanced squad", prompt)


if __name__ == "__main__":
    unittest.main()
