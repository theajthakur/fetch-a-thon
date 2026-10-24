"""MediTwin simulation engine. Pure Python (stdlib only), JSON in / JSON out.
Entry point: run_simulation(data, request) -> dict
All figures come from synthetic data and are logistics decision support, not clinical advice."""
import math
import random

# ---- Model assumptions (state these in the README) ----
PLAN_DAYS = 21                 # planning window simulated
SAFETY = 1.15                  # order enough to cover PLAN_DAYS demand x 1.15
RUNS = 1000                    # Monte Carlo runs
UNRELIABLE_DELAY_DAYS = 3      # extra delay when a supplier fails to deliver on time
EXPEDITE_LEAD_FACTOR = 0.5     # expedite halves lead time ...
EXPEDITE_COST_FACTOR = 1.2     # ... for +20% unit cost
TRANSFER_COST_INR = 1500       # logistics cost per inter-hospital transfer
OK_PROB = 0.05                 # a plan is "safe" if stockout probability <= 5%


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _num(x, default=0.0):
    try:
        v = float(x)
        return default if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return default


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


def _monte_carlo(stock, mean, std, inflows, runs, seed):
    """inflows: [{'day': int, 'qty': float, 'late_prob': float}]. Same seed => fair option comparison."""
    rng = random.Random(seed)
    stockout_runs, unmet_sum, first_days = 0, 0.0, []
    for _ in range(runs):
        arrivals = {}
        for f in inflows:
            if f["qty"] <= 0:
                continue
            d = int(max(1, math.ceil(f["day"])))
            if f.get("late_prob", 0) > 0 and rng.random() < f["late_prob"]:
                d += UNRELIABLE_DELAY_DAYS
            arrivals[d] = arrivals.get(d, 0) + f["qty"]
        s, unmet, first = stock, 0.0, None
        for day in range(1, PLAN_DAYS + 1):
            s += arrivals.get(day, 0)
            demand = max(0.0, rng.gauss(mean, std))
            if demand > s:
                unmet += demand - s
                s = 0.0
                if first is None:
                    first = day
            else:
                s -= demand
        if unmet > 0:
            stockout_runs += 1
            first_days.append(first)
        unmet_sum += unmet
    return {"stockout_prob": round(stockout_runs / runs, 3),
            "expected_unmet_units": round(unmet_sum / runs, 1),
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


def _build_options(data, item, eff, prefs, runs, seed):
    opts = []
    budget = _num(prefs.get("budget_cap_inr"), 0)
    pref = prefs.get("preferred_suppliers") or []

    def add(oid, typ, label, sid, qty, lead, cost, inflows):
        m = _monte_carlo(eff["stock"], eff["mean"], eff["std"], inflows, runs, seed)
        opts.append({"id": oid, "type": typ, "label": label, "supplier_id": sid, "qty": int(qty),
                     "lead_days": lead, "total_cost_inr": int(round(cost)),
                     "within_budget": (budget <= 0 or cost <= budget),
                     "preferred": (sid in pref) if sid else False,
                     "stockout_prob": m["stockout_prob"],
                     "expected_unmet_units": m["expected_unmet_units"]})

    transfers = []
    for hid, h in (data.get("sister_hospitals") or {}).items():
        spare = int(_num((h.get("spare_units") or {}).get(item["id"]), 0))
        if spare > 0:
            day = max(1, int(math.ceil(_num(h.get("transfer_hours"), 24) / 24.0)))
            transfers.append({"hid": hid, "name": h.get("name", hid), "qty": spare, "day": day})

    qty = _order_qty(eff["mean"], eff["stock"])
    for s in eff["suppliers"]:
        name = data["suppliers"].get(s["id"], {}).get("name", s["id"])
        late = round(1 - _rel(data, s["id"]), 3)
        add("order_" + s["id"], "supplier_order", "Order from %s (%dd lead)" % (name, s["lead_days"]),
            s["id"], qty, s["lead_days"], qty * s["unit_cost"],
            [{"day": s["lead_days"], "qty": qty, "late_prob": late}])
        ex_lead = max(1, int(round(s["lead_days"] * EXPEDITE_LEAD_FACTOR)))
        add("expedite_" + s["id"], "expedited_order", "Expedite from %s (%dd lead)" % (name, ex_lead),
            s["id"], qty, ex_lead, qty * s["unit_cost"] * EXPEDITE_COST_FACTOR,
            [{"day": ex_lead, "qty": qty, "late_prob": late}])

    for t in transfers:
        add("transfer_" + t["hid"], "transfer", "Transfer %d units from %s (~%dd)" % (t["qty"], t["name"], t["day"]),
            None, t["qty"], t["day"], TRANSFER_COST_INR,
            [{"day": t["day"], "qty": t["qty"], "late_prob": 0}])

    if transfers:
        tot = sum(t["qty"] for t in transfers)
        t_inflows = [{"day": t["day"], "qty": t["qty"], "late_prob": 0} for t in transfers]
        bq = _order_qty(eff["mean"], eff["stock"] + tot)
        for s in eff["suppliers"]:
            name = data["suppliers"].get(s["id"], {}).get("name", s["id"])
            late = round(1 - _rel(data, s["id"]), 3)
            add("bridge_" + s["id"], "bridge", "Bridge: transfer from sister hospitals + order from %s (%dd)" % (name, s["lead_days"]),
                s["id"], bq + tot, s["lead_days"], bq * s["unit_cost"] + TRANSFER_COST_INR * len(transfers),
                t_inflows + [{"day": s["lead_days"], "qty": bq, "late_prob": late}])
    return opts


def _recommend(opts):
    pool = [o for o in opts if o["within_budget"]] or opts
    safe = [o for o in pool if o["stockout_prob"] <= OK_PROB]
    if safe:
        best = sorted(safe, key=lambda o: (not o["preferred"], o["total_cost_inr"], o["lead_days"]))[0]
        return best["id"], "Lowest-cost plan keeping stockout risk <= %d%%" % int(OK_PROB * 100)
    best = sorted(pool, key=lambda o: (o["stockout_prob"], o["total_cost_inr"]))[0]
    return best["id"], "No plan fully removes risk; this one minimises stockout probability"


def analyze_item(data, item, sc, prefs, runs=RUNS, seed=42, with_options=False):
    eff = _effective(item, sc)
    crit = int(_clamp(_num(item["criticality"], 3), 1, 5))
    primary = _primary_supplier(eff, prefs)
    qty = _order_qty(eff["mean"], eff["stock"])
    late = round(1 - _rel(data, primary["id"]), 3)
    m = _monte_carlo(eff["stock"], eff["mean"], eff["std"],
                     [{"day": primary["lead_days"], "qty": qty, "late_prob": late}], runs, seed)
    cover = eff["stock"] / eff["mean"] if eff["mean"] > 0 else 999.0
    score = round(m["stockout_prob"] * crit / 5.0 * 100, 1)
    out = {"item_id": item["id"], "item": item["name"], "criticality": crit,
           "stock_now": int(round(eff["stock"])), "daily_demand": round(eff["mean"], 1),
           "days_of_cover": round(cover, 1),
           "primary_supplier": primary["id"], "primary_lead_days": primary["lead_days"],
           "primary_base_lead_days": primary["base_lead"],
           "shortfall_days": round(max(0.0, primary["lead_days"] - cover), 1),
           "stockout_prob": m["stockout_prob"], "expected_unmet_units": m["expected_unmet_units"],
           "expected_first_stockout_day": m["expected_first_stockout_day"],
           "risk_score": score, "severity": _severity(score)}
    if with_options:
        if m["stockout_prob"] <= 0.02:
            out["options"], out["recommended"] = [], None
            out["note"] = "No action needed: stockout risk is negligible under this scenario."
        else:
            opts = _build_options(data, item, eff, prefs, runs, seed)
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
        return {"ok": True, "scenario": sc, "preferences_used": prefs, "summary": counts,
                "items": results, "focus": focus, "warnings": warnings,
                "assumptions": {"plan_days": PLAN_DAYS, "safety_factor": SAFETY, "monte_carlo_runs": runs,
                                "unreliable_delay_days": UNRELIABLE_DELAY_DAYS,
                                "expedite": "lead x%.1f, cost x%.1f" % (EXPEDITE_LEAD_FACTOR, EXPEDITE_COST_FACTOR),
                                "transfer_cost_inr": TRANSFER_COST_INR, "data": "synthetic"}}
    except Exception as e:
        return {"ok": False, "error": "Simulation failed: %s" % e}


def format_text(res, top_n=5):
    """Plain-text summary for chat replies."""
    if not res.get("ok"):
        return "Simulation unavailable: " + res.get("error", "unknown error")
    L = ["Scenario: " + res["scenario"]["name"],
         "Risk overview: %(critical)d critical, %(high)d high, %(medium)d medium, %(low)d low" % res["summary"], ""]
    for r in res["items"][:top_n]:
        L.append("- %s: %.1f days of cover, stockout probability %d%%, risk %s (%s)" % (
            r["item"], r["days_of_cover"], r["stockout_prob"] * 100, r["risk_score"], r["severity"]))
    f = res.get("focus")
    if f:
        L += ["", "Focus: " + f["item"]]
        if f.get("note"):
            L.append(f["note"])
        for o in f.get("options", [])[:4]:
            tag = " <- recommended" if o["id"] == f.get("recommended") else ""
            L.append("  * %s | risk %d%% | Rs %s%s%s" % (
                o["label"], o["stockout_prob"] * 100, format(o["total_cost_inr"], ","),
                "" if o["within_budget"] else " (over budget)", tag))
        if f.get("recommendation_reason"):
            L.append("  Why: " + f["recommendation_reason"])
    for w in res.get("warnings", []):
        L.append("Note: " + w)
    return "\n".join(L)


if __name__ == "__main__":
    import json
    import os
    candidates = [
        os.path.join(os.path.dirname(__file__), "dataset", "data.json"),
        os.path.join(os.path.dirname(__file__), "..", "data.json"),
        "dataset/data.json",
        "src/dataset/data.json",
        "data.json",
    ]
    data_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not data_path:
        raise FileNotFoundError("Could not locate data.json in expected locations")
    with open(data_path, "r", encoding="utf-8") as fh:
        D = json.load(fh)
    print(format_text(run_simulation(D, {"scenario": "SC1", "item": "insulin"})))