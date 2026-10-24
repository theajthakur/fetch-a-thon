# MediTwin Simulation Agent (hosted on Agentverse as "meditwin-simulation").
# This file is the single source of the simulation engine; tests/test_simulation_agent.py imports it directly.
import json
from typing import List

from uagents import Agent, Context, Model

# ===== Simulation engine =====
"""MediTwin simulation engine (canonical source). Pure Python standard library, JSON in / JSON out.
Entry point: run_simulation(data, request) -> dict.
All figures come from synthetic data and are logistics decision support, not clinical advice."""
import math
import random

ENGINE_VERSION = "1.1"

# ---- Model assumptions (documented in docs/simulation-model.md) ----
PLAN_DAYS = 21                 # planning window simulated
SAFETY = 1.15                  # order enough to cover PLAN_DAYS demand x 1.15
RUNS = 1000                    # Monte Carlo runs
UNRELIABLE_DELAY_DAYS = 3      # flat extra delay when a supplier delivers late
EXPEDITE_LEAD_FACTOR = 0.5     # expedite halves lead time ...
EXPEDITE_COST_FACTOR = 1.2     # ... for +20% unit cost
TRANSFER_COST_INR = 1500       # logistics cost per inter-hospital transfer
OK_PROB = 0.05                 # a plan is "safe" if simulated stockout probability <= 5%
Z95 = 1.6448536269514722       # one-sided 95% normal quantile


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _num(x, default=0.0):
    try:
        v = float(x)
        return default if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return default


def upper95(k, n):
    """One-sided 95% upper bound on a probability after k events in n runs.
    k == 0: exact Clopper-Pearson bound 1 - 0.05**(1/n) (about 0.3% for n = 1,000). Otherwise: Wilson bound."""
    if n <= 0:
        return 1.0
    if k <= 0:
        return 1 - 0.05 ** (1.0 / n)
    p, z = k / float(n), Z95
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n))
    return min(1.0, (centre + margin) / (1 + z * z / n))


def risk_label(r):
    """Human-readable risk that never presents 'no simulated stockouts' as 'zero risk'."""
    k, n = r.get("stockout_runs"), r.get("runs")
    p = _num(r.get("stockout_prob"), 0)
    if k == 0 and n:
        return "0/%s runs (95%% bound <%.1f%%)" % (format(n, ","), _num(r.get("stockout_prob_upper95"), upper95(0, n)) * 100)
    return ("%.1f%%" % (p * 100)) if p < 0.1 else "%d%%" % round(p * 100)


# ---------------- validation & lookup ----------------
def validate_data(data):
    problems = []
    if not isinstance(data, dict):
        return ["data must be a dict"]
    for k in ("items", "suppliers"):
        if k not in data:
            problems.append("missing key: " + k)
    for it in data.get("items", []):
        iid = it.get("id", "?")
        for k in ("id", "name", "criticality", "stock", "daily_demand", "demand_std", "suppliers"):
            if k not in it:
                problems.append("item %s missing %s" % (iid, k))
        if not it.get("suppliers"):
            problems.append("item %s has no suppliers" % iid)
        if _num(it.get("daily_demand"), 0) <= 0:
            problems.append("item %s needs daily_demand > 0" % iid)
    return problems


def find_item(data, query):
    """Match by id, exact name, substring or word overlap ('insulin' -> Insulin Glargine)."""
    if not query:
        return None
    q = str(query).strip().lower()
    items = data["items"]
    for it in items:
        if it["id"].lower() == q or it["name"].lower() == q:
            return it
    for it in items:
        if q in it["name"].lower():
            return it
    words = [w for w in q.replace(",", " ").split() if len(w) > 2]
    best, best_score = None, 0
    for it in items:
        n = it["name"].lower()
        sc = sum(1 for w in words if w in n)
        if sc > best_score:
            best, best_score = it, sc
    return best


def normalize_scenario(data, scenario):
    s = scenario or {}
    if isinstance(s, str):
        s = {"id": s}
    if s.get("id"):
        for p in data.get("scenario_presets", []):
            if p.get("id") == s["id"]:
                merged = dict(p)
                merged.update({k: v for k, v in s.items() if k != "id"})
                s = merged
                break
    out = {
        "name": s.get("name", "Custom scenario" if s else "Baseline (no disruption)"),
        "supplier_id": s.get("supplier_id"),
        "delay_days": int(_clamp(_num(s.get("delay_days"), 0), 0, 90)),
        "demand_multiplier": {},
        "global_multiplier": 1.0,
        "spoilage_pct": _clamp(_num(s.get("spoilage_pct"), 0), 0, 1),
    }
    if s.get("type") == "cold_chain_failure" and out["spoilage_pct"] == 0:
        out["spoilage_pct"] = 0.25
    dm = s.get("demand_multiplier")
    if isinstance(dm, dict):
        for k, v in dm.items():
            it = find_item(data, k)
            if it:
                out["demand_multiplier"][it["id"]] = _clamp(_num(v, 1.0), 0.1, 5.0)
    elif dm is not None:
        out["global_multiplier"] = _clamp(_num(dm, 1.0), 0.1, 5.0)
    return out


# ---------------- core model ----------------
def _effective(item, sc):
    mult = sc["demand_multiplier"].get(item["id"], sc["global_multiplier"])
    stock = _num(item["stock"])
    if item.get("cold_chain") and sc["spoilage_pct"] > 0:
        stock *= (1 - sc["spoilage_pct"])
    sups = []
    for s in item["suppliers"]:
        lead = int(s["lead_days"])
        if sc["delay_days"] > 0 and sc["supplier_id"] in (None, s["id"]):
            lead += sc["delay_days"]
        sups.append({"id": s["id"], "lead_days": lead, "base_lead": int(s["lead_days"]),
                     "unit_cost": _num(s["unit_cost"])})
    return {"stock": stock, "mean": _num(item["daily_demand"]) * mult,
            "std": _num(item["demand_std"]) * mult, "suppliers": sups}


def scenario_paths(mean, std, runs, seed):
    """Common random numbers: one demand path and one supplier-delay draw per run, generated once per item
    and shared by every plan, so differences between plans come from the plans, not from sampling noise."""
    rng = random.Random(seed)
    paths = []
    for _ in range(runs):
        demand = [max(0.0, rng.gauss(mean, std)) for _ in range(PLAN_DAYS)]
        paths.append((demand, rng.random()))
    return paths


def monte_carlo(stock, paths, inflows):
    """inflows: [{'day': int, 'qty': float, 'late_prob': float}]. A supplier inflow is late in a run when that
    run's shared delay draw u < late_prob (so a less reliable supplier is late in a superset of runs)."""
    stockout_runs, unmet_sum, first_days = 0, 0.0, []
    for demand, u in paths:
        arrivals = {}
        for f in inflows:
            if f["qty"] <= 0:
                continue
            d = int(max(1, math.ceil(f["day"])))
            if f.get("late_prob", 0) > 0 and u < f["late_prob"]:
                d += UNRELIABLE_DELAY_DAYS
            arrivals[d] = arrivals.get(d, 0) + f["qty"]
        s, unmet, first = stock, 0.0, None
        for day in range(1, PLAN_DAYS + 1):
            s += arrivals.get(day, 0)
            dem = demand[day - 1]
            if dem > s:
                unmet += dem - s
                s = 0.0
                if first is None:
                    first = day
            else:
                s -= dem
        if unmet > 0:
            stockout_runs += 1
            first_days.append(first)
        unmet_sum += unmet
    n = len(paths)
    return {"stockout_prob": round(stockout_runs / float(n), 4) if n else 0.0,
            "stockout_runs": stockout_runs, "runs": n,
            "stockout_prob_upper95": round(upper95(stockout_runs, n), 4),
            "expected_unmet_units": round(unmet_sum / n, 1) if n else 0.0,
            "expected_first_stockout_day": round(sum(first_days) / len(first_days), 1) if first_days else None}


def _order_qty(mean, available):
    return int(math.ceil(max(0.0, mean * PLAN_DAYS * SAFETY - available)))


def _severity(score):
    return "critical" if score >= 60 else "high" if score >= 30 else "medium" if score >= 10 else "low"


def _primary_supplier(eff, prefs):
    pref = prefs.get("preferred_suppliers") or []
    for s in eff["suppliers"]:
        if s["id"] in pref:
            return s
    return eff["suppliers"][0]


def _rel(data, sid):
    return _num(data.get("suppliers", {}).get(sid, {}).get("reliability"), 0.9)


def _risk_fields(m):
    return {k: m[k] for k in ("stockout_prob", "stockout_runs", "runs", "stockout_prob_upper95",
                              "expected_unmet_units", "expected_first_stockout_day")}


def _build_options(data, item, eff, prefs, paths):
    """Each plan carries separate supplier and transfer quantities and costs; the same fields drive the
    simulation inflows, the recommendation and the purchase order."""
    opts = []
    budget = _num(prefs.get("budget_cap_inr"), 0)
    pref = prefs.get("preferred_suppliers") or []

    def add(oid, typ, label, sid, order_qty, unit_cost, lead, transfers_used):
        inflows = [{"day": t["day"], "qty": t["qty"], "late_prob": 0} for t in transfers_used]
        if sid and order_qty > 0:
            inflows.append({"day": lead, "qty": order_qty, "late_prob": round(1 - _rel(data, sid), 3)})
        m = monte_carlo(eff["stock"], paths, inflows)
        supplier_cost = order_qty * unit_cost
        transfer_cost = TRANSFER_COST_INR * len(transfers_used)
        total = supplier_cost + transfer_cost
        t_units = sum(t["qty"] for t in transfers_used)
        opt = {"id": oid, "type": typ, "label": label, "supplier_id": sid,
               "supplier_order_qty": int(order_qty), "supplier_unit_cost_inr": round(unit_cost, 2),
               "supplier_cost_inr": int(round(supplier_cost)),
               "transfers": [{"hid": t["hid"], "name": t["name"], "qty": t["qty"], "eta_days": t["day"],
                              "cost_inr": TRANSFER_COST_INR} for t in transfers_used],
               "transfer_units": t_units, "transfer_cost_inr": transfer_cost,
               "total_units": int(order_qty) + t_units, "total_cost_inr": int(round(total)),
               "lead_days": lead, "within_budget": (budget <= 0 or total <= budget),
               "preferred": (sid in pref) if sid else False,
               # legacy aliases kept for older Action Agent builds
               "order_qty": int(order_qty), "qty": int(order_qty) + t_units}
        opt.update(_risk_fields(m))
        opts.append(opt)

    transfers = []
    for hid, h in (data.get("sister_hospitals") or {}).items():
        spare = int(_num((h.get("spare_units") or {}).get(item["id"]), 0))
        if spare > 0:
            day = max(1, int(math.ceil(_num(h.get("transfer_hours"), 24) / 24.0)))
            transfers.append({"hid": hid, "name": h.get("name", hid), "qty": spare, "day": day})

    qty = _order_qty(eff["mean"], eff["stock"])
    for s in eff["suppliers"]:
        name = data["suppliers"].get(s["id"], {}).get("name", s["id"])
        add("order_" + s["id"], "supplier_order", "Order %d from %s (%dd lead)" % (qty, name, s["lead_days"]),
            s["id"], qty, s["unit_cost"], s["lead_days"], [])
        ex_lead = max(1, int(round(s["lead_days"] * EXPEDITE_LEAD_FACTOR)))
        add("expedite_" + s["id"], "expedited_order", "Expedite %d from %s (%dd lead)" % (qty, name, ex_lead),
            s["id"], qty, s["unit_cost"] * EXPEDITE_COST_FACTOR, ex_lead, [])

    for t in transfers:
        add("transfer_" + t["hid"], "transfer", "Transfer %d units from %s (~%dd)" % (t["qty"], t["name"], t["day"]),
            None, 0, 0.0, t["day"], [t])

    if transfers:
        tot = sum(t["qty"] for t in transfers)
        bq = _order_qty(eff["mean"], eff["stock"] + tot)
        for s in eff["suppliers"]:
            name = data["suppliers"].get(s["id"], {}).get("name", s["id"])
            add("bridge_" + s["id"], "bridge",
                "Bridge: transfer %d from sister hospitals + order %d from %s (%dd)" % (tot, bq, name, s["lead_days"]),
                s["id"], bq, s["unit_cost"], s["lead_days"], transfers)
    return opts


def _recommend(opts):
    """Never recommend a plan that leaves the item at risk when a safe plan exists.
    1. Cheapest safe plan within budget (preferred suppliers first).
    2. Otherwise the cheapest safe plan even if over budget, flagged for budget sign-off.
    3. Otherwise (no safe plan at all) the plan with the lowest simulated risk."""
    key = lambda o: (not o["preferred"], o["total_cost_inr"], o["lead_days"])
    safe = [o for o in opts if o["stockout_prob"] <= OK_PROB]
    safe_in_budget = [o for o in safe if o["within_budget"]]
    if safe_in_budget:
        best = sorted(safe_in_budget, key=key)[0]
        return best["id"], "Lowest-cost plan with simulated stockout probability <= %d%%" % int(OK_PROB * 100)
    if safe:
        best = sorted(safe, key=key)[0]
        return best["id"], ("No plan within your budget cap removes the risk; this is the lowest-cost safe plan "
                            "and needs budget sign-off")
    best = sorted(opts, key=lambda o: (o["stockout_prob"], o["total_cost_inr"]))[0]
    return best["id"], "No plan brings simulated risk under %d%%; this one minimises it" % int(OK_PROB * 100)


def analyze_item(data, item, sc, prefs, runs=RUNS, seed=42, with_options=False):
    eff = _effective(item, sc)
    crit = int(_clamp(_num(item["criticality"], 3), 1, 5))
    paths = scenario_paths(eff["mean"], eff["std"], runs, seed)
    primary = _primary_supplier(eff, prefs)
    qty = _order_qty(eff["mean"], eff["stock"])
    m = monte_carlo(eff["stock"], paths, [{"day": primary["lead_days"], "qty": qty,
                                           "late_prob": round(1 - _rel(data, primary["id"]), 3)}])
    cover = eff["stock"] / eff["mean"] if eff["mean"] > 0 else 999.0
    score = round(m["stockout_prob"] * crit / 5.0 * 100, 1)
    out = {"item_id": item["id"], "item": item["name"], "criticality": crit,
           "stock_now": int(round(eff["stock"])), "daily_demand": round(eff["mean"], 1),
           "days_of_cover": round(cover, 1),
           "primary_supplier": primary["id"], "primary_lead_days": primary["lead_days"],
           "primary_base_lead_days": primary["base_lead"],
           "shortfall_days": round(max(0.0, primary["lead_days"] - cover), 1),
           "risk_score": score, "severity": _severity(score)}
    out.update(_risk_fields(m))
    if with_options:
        if m["stockout_prob"] <= 0.02:
            out["options"], out["recommended"] = [], None
            out["note"] = "No action needed: simulated stockout risk is negligible under this scenario."
        else:
            opts = _build_options(data, item, eff, prefs, paths)
            rid, why = _recommend(opts)
            opts.sort(key=lambda o: (o["stockout_prob"], o["total_cost_inr"]))
            out["options"], out["recommended"], out["recommendation_reason"] = opts, rid, why
    return out


# ---------------- public entry point ----------------
def run_simulation(data, request=None):
    """request = {"scenario": {...} | "SC1", "item": "insulin" (optional), "preferences": {...},
                  "runs": 1000, "seed": 42}"""
    try:
        request = request or {}
        problems = validate_data(data)
        if problems:
            return {"ok": False, "error": "Invalid data: " + "; ".join(problems[:5])}
        prefs = dict(data.get("default_preferences", {}))
        prefs.update(request.get("preferences") or {})
        runs = int(_clamp(_num(request.get("runs"), RUNS), 100, 5000))
        seed = request.get("seed", 42)
        sc = normalize_scenario(data, request.get("scenario"))
        warnings, results = [], []
        for it in data["items"]:
            try:
                results.append(analyze_item(data, it, sc, prefs, runs, seed, False))
            except Exception as e:  # one bad item must not kill the whole report
                warnings.append("Skipped %s: %s" % (it.get("id", "?"), e))
        results.sort(key=lambda a: -a["risk_score"])

        focus_item = None
        if request.get("item"):
            focus_item = find_item(data, request["item"])
            if not focus_item:
                warnings.append("Item '%s' not found; showing highest-risk item instead." % request["item"])
        if not focus_item and results and results[0]["risk_score"] >= 10:
            focus_item = find_item(data, results[0]["item_id"])
        focus = analyze_item(data, focus_item, sc, prefs, runs, seed, True) if focus_item else None

        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for r in results:
            counts[r["severity"]] += 1
        return {"ok": True, "engine_version": ENGINE_VERSION, "scenario": sc, "preferences_used": prefs,
                "summary": counts, "items": results, "focus": focus, "warnings": warnings,
                "assumptions": {"plan_days": PLAN_DAYS, "safety_factor": SAFETY, "monte_carlo_runs": runs,
                                "seed": seed, "common_random_numbers": True,
                                "unreliable_delay_days": UNRELIABLE_DELAY_DAYS,
                                "expedite": "lead x%.1f, cost x%.1f" % (EXPEDITE_LEAD_FACTOR, EXPEDITE_COST_FACTOR),
                                "transfer_cost_inr": TRANSFER_COST_INR,
                                "risk_bound": "one-sided 95% upper bound (Clopper-Pearson at 0 events, Wilson otherwise)",
                                "not_modelled": ["supplier capacity limits", "minimum order quantities", "expiry",
                                                 "competition for sister-hospital stock", "delay distributions"],
                                "data": "synthetic"}}
    except Exception as e:
        return {"ok": False, "error": "Simulation failed: %s" % e}


def format_text(res, top_n=5):
    """Plain-text summary for chat replies and the CLI."""
    if not res.get("ok"):
        return "Simulation unavailable: " + res.get("error", "unknown error")
    L = ["Scenario: " + res["scenario"]["name"],
         "Risk overview: %(critical)d critical, %(high)d high, %(medium)d medium, %(low)d low" % res["summary"], ""]
    for r in res["items"][:top_n]:
        L.append("- %s: %.1f days of cover, simulated stockout %s, risk %s (%s)" % (
            r["item"], r["days_of_cover"], risk_label(r), r["risk_score"], r["severity"]))
    f = res.get("focus")
    if f:
        L += ["", "Focus: " + f["item"]]
        if f.get("note"):
            L.append(f["note"])
        for o in f.get("options", [])[:4]:
            tag = " <- recommended" if o["id"] == f.get("recommended") else ""
            L.append("  * %s | stockout %s | Rs %s%s%s" % (
                o["label"], risk_label(o), format(o["total_cost_inr"], ","),
                "" if o["within_budget"] else " (over budget)", tag))
        if f.get("recommendation_reason"):
            L.append("  Why: " + f["recommendation_reason"])
    for w in res.get("warnings", []):
        L.append("Note: " + w)
    return "\n".join(L)
# ===== End of engine =====

DATA = json.loads(r'''
{
  "hospital": {
    "id": "H1",
    "name": "City General Hospital (synthetic data)",
    "city": "Greater Noida"
  },
  "suppliers": {
    "SUP01": {
      "name": "Apex Pharma Distributors",
      "city": "Delhi",
      "reliability": 0.92,
      "cold_chain": true
    },
    "SUP02": {
      "name": "LifeLine Biologics",
      "city": "Noida",
      "reliability": 0.88,
      "cold_chain": true
    },
    "SUP03": {
      "name": "Himalaya MedSupply",
      "city": "Chandigarh",
      "reliability": 0.85,
      "cold_chain": true
    },
    "SUP04": {
      "name": "GasPro Industrial Gases",
      "city": "Ghaziabad",
      "reliability": 0.9,
      "cold_chain": false
    },
    "SUP05": {
      "name": "CityMed Wholesale",
      "city": "Delhi",
      "reliability": 0.95,
      "cold_chain": false
    },
    "SUP06": {
      "name": "SafeGuard Surgicals",
      "city": "Meerut",
      "reliability": 0.8,
      "cold_chain": false
    },
    "SUP07": {
      "name": "Bharat Generics",
      "city": "Gurugram",
      "reliability": 0.86,
      "cold_chain": false
    }
  },
  "items": [
    {
      "id": "I01",
      "name": "Insulin Glargine 100IU/mL vial",
      "unit": "vial",
      "criticality": 5,
      "cold_chain": true,
      "stock": 900,
      "daily_demand": 60,
      "demand_std": 12,
      "reorder_level": 600,
      "expiry_days": 120,
      "suppliers": [
        {
          "id": "SUP01",
          "lead_days": 7,
          "unit_cost": 450
        },
        {
          "id": "SUP02",
          "lead_days": 4,
          "unit_cost": 520
        },
        {
          "id": "SUP03",
          "lead_days": 6,
          "unit_cost": 480
        }
      ]
    },
    {
      "id": "I02",
      "name": "Hepatitis B Vaccine",
      "unit": "vial",
      "criticality": 4,
      "cold_chain": true,
      "stock": 400,
      "daily_demand": 20,
      "demand_std": 5,
      "reorder_level": 250,
      "expiry_days": 90,
      "suppliers": [
        {
          "id": "SUP02",
          "lead_days": 5,
          "unit_cost": 180
        },
        {
          "id": "SUP01",
          "lead_days": 6,
          "unit_cost": 195
        }
      ]
    },
    {
      "id": "I03",
      "name": "Oxytocin 10IU/mL ampoule",
      "unit": "ampoule",
      "criticality": 5,
      "cold_chain": true,
      "stock": 300,
      "daily_demand": 15,
      "demand_std": 4,
      "reorder_level": 200,
      "expiry_days": 100,
      "suppliers": [
        {
          "id": "SUP03",
          "lead_days": 5,
          "unit_cost": 22
        },
        {
          "id": "SUP01",
          "lead_days": 6,
          "unit_cost": 25
        }
      ]
    },
    {
      "id": "I04",
      "name": "Medical Oxygen Cylinder (D-type)",
      "unit": "cylinder",
      "criticality": 5,
      "cold_chain": false,
      "stock": 120,
      "daily_demand": 14,
      "demand_std": 4,
      "reorder_level": 80,
      "expiry_days": 3650,
      "suppliers": [
        {
          "id": "SUP04",
          "lead_days": 2,
          "unit_cost": 350
        },
        {
          "id": "SUP05",
          "lead_days": 3,
          "unit_cost": 390
        }
      ]
    },
    {
      "id": "I05",
      "name": "Adrenaline 1mg/mL ampoule",
      "unit": "ampoule",
      "criticality": 5,
      "cold_chain": false,
      "stock": 500,
      "daily_demand": 18,
      "demand_std": 5,
      "reorder_level": 250,
      "expiry_days": 300,
      "suppliers": [
        {
          "id": "SUP05",
          "lead_days": 3,
          "unit_cost": 15
        },
        {
          "id": "SUP07",
          "lead_days": 4,
          "unit_cost": 14
        }
      ]
    },
    {
      "id": "I06",
      "name": "Ceftriaxone 1g injection",
      "unit": "vial",
      "criticality": 4,
      "cold_chain": false,
      "stock": 3000,
      "daily_demand": 150,
      "demand_std": 30,
      "reorder_level": 1800,
      "expiry_days": 400,
      "suppliers": [
        {
          "id": "SUP07",
          "lead_days": 4,
          "unit_cost": 28
        },
        {
          "id": "SUP05",
          "lead_days": 3,
          "unit_cost": 31
        }
      ]
    },
    {
      "id": "I07",
      "name": "Normal Saline 0.9% 500mL",
      "unit": "bottle",
      "criticality": 4,
      "cold_chain": false,
      "stock": 4000,
      "daily_demand": 260,
      "demand_std": 50,
      "reorder_level": 2600,
      "expiry_days": 500,
      "suppliers": [
        {
          "id": "SUP05",
          "lead_days": 3,
          "unit_cost": 22
        },
        {
          "id": "SUP07",
          "lead_days": 5,
          "unit_cost": 20
        }
      ]
    },
    {
      "id": "I08",
      "name": "Heparin 5000IU vial",
      "unit": "vial",
      "criticality": 4,
      "cold_chain": false,
      "stock": 350,
      "daily_demand": 22,
      "demand_std": 6,
      "reorder_level": 220,
      "expiry_days": 250,
      "suppliers": [
        {
          "id": "SUP01",
          "lead_days": 5,
          "unit_cost": 95
        },
        {
          "id": "SUP07",
          "lead_days": 6,
          "unit_cost": 88
        }
      ]
    },
    {
      "id": "I09",
      "name": "Surgical Gloves (box of 100)",
      "unit": "box",
      "criticality": 3,
      "cold_chain": false,
      "stock": 600,
      "daily_demand": 30,
      "demand_std": 8,
      "reorder_level": 400,
      "expiry_days": 700,
      "suppliers": [
        {
          "id": "SUP06",
          "lead_days": 5,
          "unit_cost": 420
        },
        {
          "id": "SUP05",
          "lead_days": 3,
          "unit_cost": 450
        }
      ]
    },
    {
      "id": "I10",
      "name": "N95 Respirator Mask",
      "unit": "piece",
      "criticality": 3,
      "cold_chain": false,
      "stock": 2000,
      "daily_demand": 70,
      "demand_std": 15,
      "reorder_level": 1000,
      "expiry_days": 900,
      "suppliers": [
        {
          "id": "SUP06",
          "lead_days": 6,
          "unit_cost": 35
        },
        {
          "id": "SUP05",
          "lead_days": 3,
          "unit_cost": 40
        }
      ]
    },
    {
      "id": "I11",
      "name": "IV Cannula 20G",
      "unit": "piece",
      "criticality": 3,
      "cold_chain": false,
      "stock": 3500,
      "daily_demand": 120,
      "demand_std": 25,
      "reorder_level": 1800,
      "expiry_days": 800,
      "suppliers": [
        {
          "id": "SUP06",
          "lead_days": 5,
          "unit_cost": 18
        },
        {
          "id": "SUP07",
          "lead_days": 4,
          "unit_cost": 19
        }
      ]
    },
    {
      "id": "I12",
      "name": "Paracetamol 500mg (strip of 10)",
      "unit": "strip",
      "criticality": 2,
      "cold_chain": false,
      "stock": 5000,
      "daily_demand": 200,
      "demand_std": 40,
      "reorder_level": 2500,
      "expiry_days": 600,
      "suppliers": [
        {
          "id": "SUP07",
          "lead_days": 4,
          "unit_cost": 12
        },
        {
          "id": "SUP05",
          "lead_days": 3,
          "unit_cost": 13
        }
      ]
    }
  ],
  "sister_hospitals": {
    "H2": {
      "name": "District Hospital Noida",
      "transfer_hours": 4,
      "spare_units": {
        "I01": 150,
        "I02": 60,
        "I03": 40,
        "I04": 25,
        "I05": 80,
        "I06": 400,
        "I07": 600,
        "I08": 50,
        "I09": 80,
        "I10": 300,
        "I11": 500,
        "I12": 700
      }
    },
    "H3": {
      "name": "Ghaziabad Community Hospital",
      "transfer_hours": 6,
      "spare_units": {
        "I01": 80,
        "I02": 30,
        "I03": 20,
        "I04": 15,
        "I05": 50,
        "I06": 250,
        "I07": 400,
        "I08": 30,
        "I09": 50,
        "I10": 200,
        "I11": 300,
        "I12": 500
      }
    }
  },
  "scenario_presets": [
    {
      "id": "SC1",
      "name": "Supplier delay + insulin demand surge",
      "type": "supplier_delay",
      "supplier_id": "SUP01",
      "delay_days": 10,
      "demand_multiplier": {
        "I01": 1.3
      }
    },
    {
      "id": "SC2",
      "name": "Heatwave cold-chain failure",
      "type": "cold_chain_failure",
      "spoilage_pct": 0.25,
      "affects": "cold_chain_items"
    },
    {
      "id": "SC3",
      "name": "Respiratory outbreak oxygen surge",
      "type": "demand_surge",
      "demand_multiplier": {
        "I04": 1.6,
        "I10": 1.5
      }
    }
  ],
  "default_preferences": {
    "preferred_suppliers": [],
    "budget_cap_inr": 600000,
    "risk_tolerance": "low",
    "target_cover_days": 30
  }
}
''')


class SimRequest(Model):
    request_id: str
    items: List[dict]
    scenario: dict


class SimResult(Model):
    request_id: str
    results: List[dict]


agent = Agent()


def pick_data(items):
    """Use the Inventory Agent's items if they validate, otherwise the embedded snapshot."""
    if items:
        cand = dict(DATA)
        cand["items"] = items
        if not validate_data(cand):
            return cand, "inventory"
    return DATA, "embedded"


@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Simulation Agent up (engine {ENGINE_VERSION}). Address: {ctx.agent.address}")


@agent.on_message(model=SimRequest)
async def on_sim_request(ctx: Context, sender: str, msg: SimRequest):
    ctx.logger.info(f"SimRequest {msg.request_id} with {len(msg.items)} items")
    try:
        scenario = dict(msg.scenario or {})
        item = scenario.pop("item", None)
        prefs = scenario.pop("preferences", None) or {}
        req = {"scenario": scenario or None, "item": item, "preferences": prefs}
        data, src = pick_data(msg.items)
        res = run_simulation(data, req)
        if src == "inventory" and (not res.get("ok") or not res.get("items")):
            data, src = DATA, "embedded (inventory data unusable)"
            res = run_simulation(data, req)
        if not res.get("ok"):
            results = [{"error": res.get("error", "unknown error")}]
        else:
            focus = res.get("focus")
            if focus and focus.get("options"):
                rec, top = focus.get("recommended"), focus["options"][:6]
                if rec and all(o["id"] != rec for o in top):
                    top = top[:5] + [o for o in focus["options"] if o["id"] == rec]
                focus["options"] = top
            results = list(res["items"]) + [{"meta": {
                "scenario": res["scenario"], "summary": res["summary"], "focus": focus,
                "warnings": res.get("warnings", []), "data_source": src,
                "engine_version": res.get("engine_version")}}]
    except Exception as e:
        ctx.logger.error(f"Simulation failed: {e}")
        results = [{"error": f"Simulation agent error: {e}"}]
    await ctx.send(sender, SimResult(request_id=msg.request_id, results=results))
    ctx.logger.info(f"Replied with SimResult {msg.request_id}")