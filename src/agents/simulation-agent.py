import json
import math
import random
from typing import List
from uagents import Agent, Context, Model

PLAN_DAYS = 21
SAFETY = 1.15
RUNS = 1000
UNRELIABLE_DELAY_DAYS = 3
EXPEDITE_LEAD_FACTOR = 0.5
EXPEDITE_COST_FACTOR = 1.2
TRANSFER_COST_INR = 1500
OK_PROB = 0.05


def _clamp(val, low, high):
    return max(low, min(high, val))


def _to_float(val, default=0.0):
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def validate_data(data):
    issues = []
    if not isinstance(data, dict):
        return ["data must be a dict"]
    for key in ("items", "suppliers"):
        if key not in data:
            issues.append(f"missing key: {key}")
    for item in data.get("items", []):
        iid = item.get("id", "?")
        for key in ("id", "name", "criticality", "stock", "daily_demand", "demand_std", "suppliers"):
            if key not in item:
                issues.append(f"item {iid} missing {key}")
        if not item.get("suppliers"):
            issues.append(f"item {iid} has no suppliers")
        if _to_float(item.get("daily_demand"), 0) <= 0:
            issues.append(f"item {iid} needs daily_demand > 0")
    return issues


def find_item(data, query):
    if not query:
        return None
    raw = str(query).strip().lower()
    items = data.get("items", [])

    for item in items:
        if item["id"].lower() == raw or item["name"].lower() == raw:
            return item

    for item in items:
        if raw in item["name"].lower():
            return item

    words = [w for w in raw.replace(",", " ").split() if len(w) > 2]
    matched, top_score = None, 0
    for item in items:
        name_lower = item["name"].lower()
        score = sum(1 for w in words if w in name_lower)
        if score > top_score:
            matched, top_score = item, score
    return matched


def normalize_scenario(data, scenario):
    cfg = scenario or {}
    if isinstance(cfg, str):
        cfg = {"id": cfg}
    if cfg.get("id"):
        for preset in data.get("scenario_presets", []):
            if preset.get("id") == cfg["id"]:
                merged = dict(preset)
                merged.update({k: v for k, v in cfg.items() if k != "id"})
                cfg = merged
                break

    parsed = {
        "name": cfg.get("name", "Custom scenario" if cfg else "Baseline (no disruption)"),
        "supplier_id": cfg.get("supplier_id"),
        "delay_days": int(_clamp(_to_float(cfg.get("delay_days"), 0), 0, 90)),
        "demand_multiplier": {},
        "global_multiplier": 1.0,
        "spoilage_pct": _clamp(_to_float(cfg.get("spoilage_pct"), 0), 0, 1),
    }

    if cfg.get("type") == "cold_chain_failure" and parsed["spoilage_pct"] == 0:
        parsed["spoilage_pct"] = 0.25

    dm = cfg.get("demand_multiplier")
    if isinstance(dm, dict):
        for k, v in dm.items():
            matched = find_item(data, k)
            if matched:
                parsed["demand_multiplier"][matched["id"]] = _clamp(_to_float(v, 1.0), 0.1, 5.0)
    elif dm is not None:
        parsed["global_multiplier"] = _clamp(_to_float(dm, 1.0), 0.1, 5.0)

    return parsed


def _effective(item, sc):
    mult = sc["demand_multiplier"].get(item["id"], sc["global_multiplier"])
    stock = _to_float(item["stock"])
    if item.get("cold_chain") and sc["spoilage_pct"] > 0:
        stock *= (1 - sc["spoilage_pct"])

    suppliers = []
    for s in item["suppliers"]:
        lead = int(s["lead_days"])
        if sc["delay_days"] > 0 and sc["supplier_id"] in (None, s["id"]):
            lead += sc["delay_days"]
        suppliers.append({
            "id": s["id"],
            "lead_days": lead,
            "base_lead": int(s["lead_days"]),
            "unit_cost": _to_float(s["unit_cost"]),
        })

    return {
        "stock": stock,
        "mean": _to_float(item["daily_demand"]) * mult,
        "std": _to_float(item["demand_std"]) * mult,
        "suppliers": suppliers,
    }


def _monte_carlo(stock, mean, std, inflows, runs, seed):
    rng = random.Random(seed)
    stockout_count = 0
    total_unmet = 0.0
    first_stockout_days = []

    for _ in range(runs):
        arrivals = {}
        for flow in inflows:
            if flow["qty"] <= 0:
                continue
            day = int(max(1, math.ceil(flow["day"])))
            if flow.get("late_prob", 0) > 0 and rng.random() < flow["late_prob"]:
                day += UNRELIABLE_DELAY_DAYS
            arrivals[day] = arrivals.get(day, 0) + flow["qty"]

        curr_stock = stock
        unmet = 0.0
        first_day = None

        for day in range(1, PLAN_DAYS + 1):
            curr_stock += arrivals.get(day, 0)
            demand = max(0.0, rng.gauss(mean, std))
            if demand > curr_stock:
                unmet += demand - curr_stock
                curr_stock = 0.0
                if first_day is None:
                    first_day = day
            else:
                curr_stock -= demand

        if unmet > 0:
            stockout_count += 1
            first_stockout_days.append(first_day)
        total_unmet += unmet

    return {
        "stockout_prob": round(stockout_count / runs, 3),
        "expected_unmet_units": round(total_unmet / runs, 1),
        "expected_first_stockout_day": (
            round(sum(first_stockout_days) / len(first_stockout_days), 1)
            if first_stockout_days else None
        ),
    }


def _order_qty(mean, available):
    return int(math.ceil(max(0.0, mean * PLAN_DAYS * SAFETY - available)))


def _severity(score):
    if score >= 60:
        return "critical"
    if score >= 30:
        return "high"
    if score >= 10:
        return "medium"
    return "low"


def _primary_supplier(eff, prefs):
    preferred = prefs.get("preferred_suppliers") or []
    for s in eff["suppliers"]:
        if s["id"] in preferred:
            return s
    return eff["suppliers"][0]


def _rel(data, sid):
    return _to_float(data.get("suppliers", {}).get(sid, {}).get("reliability"), 0.9)


def _build_options(data, item, eff, prefs, runs, seed):
    options = []
    budget = _to_float(prefs.get("budget_cap_inr"), 0)
    preferred = prefs.get("preferred_suppliers") or []

    def append_opt(oid, typ, label, sid, qty, lead, cost, inflows):
        sim = _monte_carlo(eff["stock"], eff["mean"], eff["std"], inflows, runs, seed)
        options.append({
            "id": oid,
            "type": typ,
            "label": label,
            "supplier_id": sid,
            "qty": int(qty),
            "lead_days": lead,
            "total_cost_inr": int(round(cost)),
            "within_budget": (budget <= 0 or cost <= budget),
            "preferred": (sid in preferred) if sid else False,
            "stockout_prob": sim["stockout_prob"],
            "expected_unmet_units": sim["expected_unmet_units"],
        })

    transfers = []
    for hid, hospital in (data.get("sister_hospitals") or {}).items():
        spare = int(_to_float((hospital.get("spare_units") or {}).get(item["id"]), 0))
        if spare > 0:
            day = max(1, int(math.ceil(_to_float(hospital.get("transfer_hours"), 24) / 24.0)))
            transfers.append({"hid": hid, "name": hospital.get("name", hid), "qty": spare, "day": day})

    qty = _order_qty(eff["mean"], eff["stock"])
    for s in eff["suppliers"]:
        name = data["suppliers"].get(s["id"], {}).get("name", s["id"])
        late = round(1 - _rel(data, s["id"]), 3)
        append_opt(
            f"order_{s['id']}", "supplier_order", f"Order from {name} ({s['lead_days']}d lead)",
            s["id"], qty, s["lead_days"], qty * s["unit_cost"],
            [{"day": s["lead_days"], "qty": qty, "late_prob": late}],
        )
        ex_lead = max(1, int(round(s["lead_days"] * EXPEDITE_LEAD_FACTOR)))
        append_opt(
            f"expedite_{s['id']}", "expedited_order", f"Expedite from {name} ({ex_lead}d lead)",
            s["id"], qty, ex_lead, qty * s["unit_cost"] * EXPEDITE_COST_FACTOR,
            [{"day": ex_lead, "qty": qty, "late_prob": late}],
        )

    for t in transfers:
        append_opt(
            f"transfer_{t['hid']}", "transfer", f"Transfer {t['qty']} units from {t['name']} (~{t['day']}d)",
            None, t["qty"], t["day"], TRANSFER_COST_INR,
            [{"day": t["day"], "qty": t["qty"], "late_prob": 0}],
        )

    if transfers:
        total_transfer_qty = sum(t["qty"] for t in transfers)
        transfer_inflows = [{"day": t["day"], "qty": t["qty"], "late_prob": 0} for t in transfers]
        bridge_qty = _order_qty(eff["mean"], eff["stock"] + total_transfer_qty)
        for s in eff["suppliers"]:
            name = data["suppliers"].get(s["id"], {}).get("name", s["id"])
            late = round(1 - _rel(data, s["id"]), 3)
            append_opt(
                f"bridge_{s['id']}", "bridge",
                f"Bridge: transfer from sister hospitals + order from {name} ({s['lead_days']}d)",
                s["id"], bridge_qty + total_transfer_qty, s["lead_days"],
                bridge_qty * s["unit_cost"] + TRANSFER_COST_INR * len(transfers),
                transfer_inflows + [{"day": s["lead_days"], "qty": bridge_qty, "late_prob": late}],
            )

    return options


def _recommend(opts):
    eligible = [o for o in opts if o["within_budget"]] or opts
    safe = [o for o in eligible if o["stockout_prob"] <= OK_PROB]
    if safe:
        best = sorted(safe, key=lambda o: (not o["preferred"], o["total_cost_inr"], o["lead_days"]))[0]
        return best["id"], f"Lowest-cost plan keeping stockout risk <= {int(OK_PROB * 100)}%"
    best = sorted(eligible, key=lambda o: (o["stockout_prob"], o["total_cost_inr"]))[0]
    return best["id"], "No plan fully removes risk; this one minimises stockout probability"


def analyze_item(data, item, sc, prefs, runs=RUNS, seed=42, with_options=False):
    eff = _effective(item, sc)
    crit = int(_clamp(_to_float(item["criticality"], 3), 1, 5))
    primary = _primary_supplier(eff, prefs)
    qty = _order_qty(eff["mean"], eff["stock"])
    late = round(1 - _rel(data, primary["id"]), 3)

    sim = _monte_carlo(
        eff["stock"], eff["mean"], eff["std"],
        [{"day": primary["lead_days"], "qty": qty, "late_prob": late}],
        runs, seed,
    )
    cover = eff["stock"] / eff["mean"] if eff["mean"] > 0 else 999.0
    risk = round(sim["stockout_prob"] * crit / 5.0 * 100, 1)

    result = {
        "item_id": item["id"],
        "item": item["name"],
        "criticality": crit,
        "stock_now": int(round(eff["stock"])),
        "daily_demand": round(eff["mean"], 1),
        "days_of_cover": round(cover, 1),
        "primary_supplier": primary["id"],
        "primary_lead_days": primary["lead_days"],
        "primary_base_lead_days": primary["base_lead"],
        "shortfall_days": round(max(0.0, primary["lead_days"] - cover), 1),
        "stockout_prob": sim["stockout_prob"],
        "expected_unmet_units": sim["expected_unmet_units"],
        "expected_first_stockout_day": sim["expected_first_stockout_day"],
        "risk_score": risk,
        "severity": _severity(risk),
    }

    if with_options:
        if sim["stockout_prob"] <= 0.02:
            result["options"], result["recommended"] = [], None
            result["note"] = "No action needed: stockout risk is negligible under this scenario."
        else:
            opts = _build_options(data, item, eff, prefs, runs, seed)
            opt_id, reason = _recommend(opts)
            opts.sort(key=lambda o: (o["stockout_prob"], o["total_cost_inr"]))
            result["options"] = opts
            result["recommended"] = opt_id
            result["recommendation_reason"] = reason

    return result


def run_simulation(data, request=None):
    try:
        request = request or {}
        issues = validate_data(data)
        if issues:
            return {"ok": False, "error": f"Invalid data: {'; '.join(issues[:5])}"}

        prefs = dict(data.get("default_preferences", {}))
        prefs.update(request.get("preferences") or {})

        runs = int(_clamp(_to_float(request.get("runs"), RUNS), 100, 5000))
        seed = request.get("seed", 42)
        sc = normalize_scenario(data, request.get("scenario"))

        warnings, results = [], []
        for item in data.get("items", []):
            try:
                results.append(analyze_item(data, item, sc, prefs, runs, seed, False))
            except Exception as exc:
                warnings.append(f"Skipped {item.get('id', '?')}: {exc}")

        results.sort(key=lambda x: -x["risk_score"])

        focus_item = None
        if request.get("item"):
            focus_item = find_item(data, request["item"])
            if not focus_item:
                warnings.append(f"Item '{request['item']}' not found; showing highest-risk item instead.")

        if not focus_item and results and results[0]["risk_score"] >= 10:
            focus_item = find_item(data, results[0]["item_id"])

        focus = analyze_item(data, focus_item, sc, prefs, runs, seed, True) if focus_item else None

        summary = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for r in results:
            summary[r["severity"]] += 1

        return {
            "ok": True,
            "scenario": sc,
            "preferences_used": prefs,
            "summary": summary,
            "items": results,
            "focus": focus,
            "warnings": warnings,
            "assumptions": {
                "plan_days": PLAN_DAYS,
                "safety_factor": SAFETY,
                "monte_carlo_runs": runs,
                "unreliable_delay_days": UNRELIABLE_DELAY_DAYS,
                "expedite": f"lead x{EXPEDITE_LEAD_FACTOR:.1f}, cost x{EXPEDITE_COST_FACTOR:.1f}",
                "transfer_cost_inr": TRANSFER_COST_INR,
                "data": "synthetic",
            },
        }
    except Exception as exc:
        return {"ok": False, "error": f"Simulation failed: {exc}"}


def format_text(res, top_n=5):
    if not res.get("ok"):
        return f"Simulation unavailable: {res.get('error', 'unknown error')}"

    lines = [
        f"Scenario: {res['scenario']['name']}",
        f"Risk overview: {res['summary']['critical']} critical, {res['summary']['high']} high, "
        f"{res['summary']['medium']} medium, {res['summary']['low']} low",
        "",
    ]
    for r in res["items"][:top_n]:
        lines.append(
            f"- {r['item']}: {r['days_of_cover']:.1f} days of cover, stockout probability "
            f"{int(r['stockout_prob'] * 100)}%, risk {r['risk_score']} ({r['severity']})"
        )

    focus = res.get("focus")
    if focus:
        lines.extend(["", f"Focus: {focus['item']}"])
        if focus.get("note"):
            lines.append(focus["note"])
        for opt in focus.get("options", [])[:4]:
            rec_tag = " <- recommended" if opt["id"] == focus.get("recommended") else ""
            budget_str = "" if opt["within_budget"] else " (over budget)"
            lines.append(
                f"  * {opt['label']} | risk {int(opt['stockout_prob'] * 100)}% | "
                f"Rs {opt['total_cost_inr']:,}{budget_str}{rec_tag}"
            )
        if focus.get("recommendation_reason"):
            lines.append(f"  Why: {focus['recommendation_reason']}")

    for w in res.get("warnings", []):
        lines.append(f"Note: {w}")

    return "\n".join(lines)


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
            "reliability": 0.90,
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
            "reliability": 0.80,
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
            "piece": "piece",
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
    if items:
        candidate = dict(DATA)
        candidate["items"] = items
        if not validate_data(candidate):
            return candidate, "inventory"
    return DATA, "embedded"


@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Simulation agent online: {ctx.agent.address}")


@agent.on_message(model=SimRequest)
async def on_sim_request(ctx: Context, sender: str, msg: SimRequest):
    ctx.logger.info(f"SimRequest {msg.request_id} received ({len(msg.items)} items)")
    try:
        scenario = dict(msg.scenario or {})
        item = scenario.pop("item", None)
        prefs = scenario.pop("preferences", None) or {}
        req = {"scenario": scenario or None, "item": item, "preferences": prefs}

        data, src = pick_data(msg.items)
        res = run_simulation(data, req)
        if src == "inventory" and (not res.get("ok") or not res.get("items")):
            data, src = DATA, "embedded"
            res = run_simulation(data, req)

        if not res.get("ok"):
            results = [{"error": res.get("error", "unknown error")}]
        else:
            focus = res.get("focus")
            if focus and focus.get("options"):
                focus["options"] = focus["options"][:6]
            results = list(res["items"]) + [{
                "meta": {
                    "scenario": res["scenario"],
                    "summary": res["summary"],
                    "focus": focus,
                    "warnings": res.get("warnings", []),
                    "data_source": src,
                }
            }]
    except Exception as exc:
        ctx.logger.exception("Simulation execution failed")
        results = [{"error": f"Simulation agent error: {exc}"}]

    await ctx.send(sender, SimResult(request_id=msg.request_id, results=results))
    ctx.logger.info(f"SimResult {msg.request_id} returned to {sender}")