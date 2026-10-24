"""MediTwin Inventory Agent: holds the hospital's live stock twin (synthetic data) and serves
snapshots in the exact schema the Simulation Agent validates."""
from datetime import datetime, timezone
from typing import List

from uagents import Agent, Context, Model


# ---- message models (must match the Action Agent exactly) ----
class StockRequest(Model):
    request_id: str
    focus_items: List[str] = []


class StockSnapshot(Model):
    request_id: str
    items: List[dict]
    synthetic: bool = True


SUPPLIER_NAMES = {
    "SUP01": "Apex Pharma Distributors", "SUP02": "LifeLine Biologics", "SUP03": "Himalaya MedSupply",
    "SUP04": "GasPro Industrial Gases", "SUP05": "CityMed Wholesale", "SUP06": "SafeGuard Surgicals",
    "SUP07": "Bharat Generics",
}

# (id, name, unit, criticality, cold_chain, stock, daily_demand, demand_std, reorder_level,
#  [(supplier_id, lead_days, unit_cost_inr), ...])   -- identical to data.json
RAW_ITEMS = [
    ("I01", "Insulin Glargine 100IU/mL vial", "vial", 5, True, 900, 60, 12, 600,
     [("SUP01", 7, 450), ("SUP02", 4, 520), ("SUP03", 6, 480)]),
    ("I02", "Hepatitis B Vaccine", "vial", 4, True, 400, 20, 5, 250, [("SUP02", 5, 180), ("SUP01", 6, 195)]),
    ("I03", "Oxytocin 10IU/mL ampoule", "ampoule", 5, True, 300, 15, 4, 200, [("SUP03", 5, 22), ("SUP01", 6, 25)]),
    ("I04", "Medical Oxygen Cylinder (D-type)", "cylinder", 5, False, 120, 14, 4, 80,
     [("SUP04", 2, 350), ("SUP05", 3, 390)]),
    ("I05", "Adrenaline 1mg/mL ampoule", "ampoule", 5, False, 500, 18, 5, 250, [("SUP05", 3, 15), ("SUP07", 4, 14)]),
    ("I06", "Ceftriaxone 1g injection", "vial", 4, False, 3000, 150, 30, 1800, [("SUP07", 4, 28), ("SUP05", 3, 31)]),
    ("I07", "Normal Saline 0.9% 500mL", "bottle", 4, False, 4000, 260, 50, 2600, [("SUP05", 3, 22), ("SUP07", 5, 20)]),
    ("I08", "Heparin 5000IU vial", "vial", 4, False, 350, 22, 6, 220, [("SUP01", 5, 95), ("SUP07", 6, 88)]),
    ("I09", "Surgical Gloves (box of 100)", "box", 3, False, 600, 30, 8, 400, [("SUP06", 5, 420), ("SUP05", 3, 450)]),
    ("I10", "N95 Respirator Mask", "piece", 3, False, 2000, 70, 15, 1000, [("SUP06", 6, 35), ("SUP05", 3, 40)]),
    ("I11", "IV Cannula 20G", "piece", 3, False, 3500, 120, 25, 1800, [("SUP06", 5, 18), ("SUP07", 4, 19)]),
    ("I12", "Paracetamol 500mg (strip of 10)", "strip", 2, False, 5000, 200, 40, 2500,
     [("SUP07", 4, 12), ("SUP05", 3, 13)]),
]


def build_items():
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for iid, name, unit, crit, cold, stock, dd, std, rl, sups in RAW_ITEMS:
        out.append({
            "id": iid, "name": name, "unit": unit, "criticality": crit, "cold_chain": cold,
            "stock": stock, "daily_demand": dd, "demand_std": std, "reorder_level": rl,
            "below_reorder": stock <= rl, "as_of": as_of,
            "suppliers": [{"id": s, "name": SUPPLIER_NAMES.get(s, s), "lead_days": l, "unit_cost": c}
                          for s, l, c in sups],
        })
    return out


def filter_items(items, focus):
    """Optional filter by id or name fragment; empty or no-match -> full list (Simulation needs all items)."""
    if not focus:
        return items
    f = [q.strip().lower() for q in focus if q and q.strip()]
    hit = [it for it in items if any(q == it["id"].lower() or q in it["name"].lower() for q in f)]
    return hit or items


agent = Agent()


@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Inventory Agent up. Address: {ctx.agent.address} | {len(RAW_ITEMS)} items loaded")


@agent.on_message(model=StockRequest)
async def on_stock_request(ctx: Context, sender: str, msg: StockRequest):
    ctx.logger.info(f"StockRequest {msg.request_id} from {sender} focus={msg.focus_items}")
    try:
        items = filter_items(build_items(), msg.focus_items)
    except Exception as e:  # never leave the Action Agent hanging
        ctx.logger.error(f"Snapshot build failed: {e}")
        items = []  # Simulation Agent falls back to its embedded snapshot
    await ctx.send(sender, StockSnapshot(request_id=msg.request_id, items=items, synthetic=True))
    ctx.logger.info(f"Replied StockSnapshot {msg.request_id} with {len(items)} items")