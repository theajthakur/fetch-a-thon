"""Repeatable tests for the MediTwin engine embedded in src/agents/simulation-agent.py.
Standard library only (uagents is stubbed): python -m unittest discover -s tests -v"""
import importlib.util
import json
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_FILE = os.path.join(ROOT, "src", "agents", "simulation-agent.py")


def _load_engine():
    """Import the hosted agent file without uagents installed, so the exact deployed code is tested."""
    stub = types.ModuleType("uagents")

    class Model:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Agent:
        def __init__(self, *a, **kw):
            pass

        def __getattr__(self, name):
            return lambda *a, **kw: (lambda f: f)

    stub.Model, stub.Agent, stub.Context = Model, Agent, object
    sys.modules.setdefault("uagents", stub)
    spec = importlib.util.spec_from_file_location("simulation_agent", AGENT_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sim = _load_engine()
DATA = sim.DATA


def tiny(stock=100, demand=10, std=0, lead=20, reliability=1.0, spare=0):
    """Hand-checkable one-item world."""
    d = {"suppliers": {"S1": {"name": "S1", "reliability": reliability}},
         "items": [{"id": "X", "name": "Test item", "criticality": 5, "stock": stock, "daily_demand": demand,
                    "demand_std": std, "suppliers": [{"id": "S1", "lead_days": lead, "unit_cost": 10}]}],
         "default_preferences": {"budget_cap_inr": 0}}
    if spare:
        d["sister_hospitals"] = {"H": {"name": "H", "transfer_hours": 4, "spare_units": {"X": spare}}}
    return d


class HandCalculated(unittest.TestCase):
    def test_deterministic_stockout(self):
        # 100 units, 10/day, no variance, order lands day 20: days 11..19 short by 10 each -> 90 unmet.
        f = sim.run_simulation(tiny(), {"item": "X"})["focus"]
        self.assertEqual(f["days_of_cover"], 10.0)
        self.assertEqual(f["stockout_prob"], 1.0)
        self.assertEqual(f["expected_unmet_units"], 90.0)
        self.assertEqual(f["expected_first_stockout_day"], 11)

    def test_covered_item_has_no_stockout(self):
        f = sim.run_simulation(tiny(stock=1000, lead=2), {"item": "X"})["focus"]
        self.assertEqual(f["stockout_runs"], 0)
        self.assertEqual(f["options"], [])

    def test_late_supplier_always_late(self):
        # reliability 0 -> every delivery 3 days late; cover 10 days, lead 9 -> arrives day 12 -> short days 11.
        f = sim.run_simulation(tiny(lead=9, reliability=0.0), {"item": "X"})["focus"]
        self.assertEqual(f["stockout_prob"], 1.0)
        self.assertEqual(f["expected_first_stockout_day"], 11)


class Statistics(unittest.TestCase):
    def test_zero_event_bound(self):
        self.assertAlmostEqual(sim.upper95(0, 1000), 0.002991, places=5)

    def test_bound_is_above_estimate(self):
        for k in (1, 50, 500, 999):
            self.assertGreater(sim.upper95(k, 1000), k / 1000.0)

    def test_label_never_says_zero_percent(self):
        r = {"stockout_prob": 0.0, "stockout_runs": 0, "runs": 1000, "stockout_prob_upper95": 0.003}
        self.assertNotIn(" 0%", " " + sim.risk_label(r))
        self.assertIn("0/1,000 runs", sim.risk_label(r))


class HeroScenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = sim.run_simulation(DATA, {"scenario": "SC1", "item": "insulin"})
        cls.focus = cls.res["focus"]
        cls.opts = {o["id"]: o for o in cls.focus["options"]}

    def test_insulin_critical(self):
        self.assertEqual(self.focus["severity"], "critical")
        self.assertEqual(self.focus["days_of_cover"], 11.5)
        self.assertEqual(self.focus["primary_lead_days"], 17)

    def test_recommendation(self):
        self.assertEqual(self.focus["recommended"], "bridge_SUP03")
        b = self.opts["bridge_SUP03"]
        self.assertEqual(b["supplier_order_qty"], 754)
        self.assertEqual(b["transfer_units"], 230)
        self.assertEqual(b["total_cost_inr"], 364920)

    def test_cost_accounting_is_consistent(self):
        for o in self.focus["options"]:
            self.assertEqual(o["supplier_cost_inr"], round(o["supplier_order_qty"] * o["supplier_unit_cost_inr"]), o["id"])
            self.assertEqual(o["transfer_cost_inr"], sum(t["cost_inr"] for t in o["transfers"]), o["id"])
            self.assertEqual(o["total_cost_inr"], o["supplier_cost_inr"] + o["transfer_cost_inr"], o["id"])
            self.assertEqual(o["transfer_units"], sum(t["qty"] for t in o["transfers"]), o["id"])
            self.assertEqual(o["total_units"], o["supplier_order_qty"] + o["transfer_units"], o["id"])

    def test_cascade_to_heparin(self):
        hep = next(r for r in self.res["items"] if r["item_id"] == "I08")
        self.assertGreater(hep["stockout_prob"], 0.0)

    def test_reproducible(self):
        again = sim.run_simulation(DATA, {"scenario": "SC1", "item": "insulin"})
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(self.res, sort_keys=True))


class CommonRandomNumbers(unittest.TestCase):
    def test_more_stock_never_hurts(self):
        paths = sim.scenario_paths(60, 12, 500, 7)
        low = sim.monte_carlo(500, paths, [])
        high = sim.monte_carlo(900, paths, [])
        self.assertLessEqual(high["stockout_runs"], low["stockout_runs"])
        self.assertLessEqual(high["expected_unmet_units"], low["expected_unmet_units"])

    def test_extra_transfer_never_hurts(self):
        paths = sim.scenario_paths(60, 12, 500, 7)
        base = sim.monte_carlo(600, paths, [{"day": 9, "qty": 800, "late_prob": 0.15}])
        plus = sim.monte_carlo(600, paths, [{"day": 9, "qty": 800, "late_prob": 0.15}, {"day": 1, "qty": 200, "late_prob": 0}])
        self.assertLessEqual(plus["stockout_runs"], base["stockout_runs"])


class Robustness(unittest.TestCase):
    def test_invalid_data(self):
        self.assertFalse(sim.run_simulation({"items": []}, {})["ok"])

    def test_clamping(self):
        sc = sim.normalize_scenario(DATA, {"delay_days": 999, "demand_multiplier": 50, "spoilage_pct": 3})
        self.assertEqual((sc["delay_days"], sc["global_multiplier"], sc["spoilage_pct"]), (90, 5.0, 1))

    def test_unknown_item_warns(self):
        res = sim.run_simulation(DATA, {"scenario": "SC1", "item": "unobtainium"})
        self.assertTrue(res["ok"])
        self.assertTrue(any("not found" in w for w in res["warnings"]))

    def test_fuzzy_item(self):
        self.assertEqual(sim.find_item(DATA, "O2 cylinder")["id"], "I04")


class DatasetInSync(unittest.TestCase):
    def test_embedded_data_matches_dataset_file(self):
        path = os.path.join(ROOT, "src", "dataset", "data.json")
        self.assertEqual(sim.DATA, json.load(open(path, encoding="utf-8")),
                         "src/dataset/data.json and the DATA embedded in simulation-agent.py differ")


if __name__ == "__main__":
    unittest.main()
