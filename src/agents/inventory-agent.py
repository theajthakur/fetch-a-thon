from datetime import datetime, timezone
from typing import List
from uagents import Agent, Context, Model


class StockRequest(Model):
    request_id: str
    focus_items: List[str] = []


class StockSnapshot(Model):
    request_id: str
    items: List[dict]
    synthetic: bool = True


SUPPLIER_NAMES = {
    "SUP01": "Apex Pharma Distributors",
    "SUP02": "LifeLine Biologics",
    "SUP03": "Himalaya MedSupply",
    "SUP04": "GasPro Industrial Gases",
    "SUP05": "CityMed Wholesale",
    "SUP06": "SafeGuard Surgicals",
    "SUP07": "Bharat Generics",
}

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
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    catalog = []
    for item_id, name, unit, crit, cold, stock, dd, std, reorder, suppliers in RAW_ITEMS:
        catalog.append({
            "id": item_id,
            "name": name,
            "unit": unit,
            "criticality": crit,
            "cold_chain": cold,
            "stock": stock,
            "daily_demand": dd,
            "demand_std": std,
            "reorder_level": reorder,
            "below_reorder": stock <= reorder,
            "as_of": timestamp,
            "suppliers": [
                {
                    "id": sup_id,
                    "name": SUPPLIER_NAMES.get(sup_id, sup_id),
                    "lead_days": lead,
                    "unit_cost": cost,
                }
                for sup_id, lead, cost in suppliers
            ],
        })
    return catalog


def filter_items(items, focus):
    if not focus:
        return items
    queries = [q.strip().lower() for q in focus if q and q.strip()]
    if not queries:
        return items
    matched = [
        item for item in items
        if any(q == item["id"].lower() or q in item["name"].lower() for q in queries)
    ]
    return matched or items


agent = Agent()


@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Inventory agent online: {ctx.agent.address} ({len(RAW_ITEMS)} items)")


@agent.on_message(model=StockRequest)
async def on_stock_request(ctx: Context, sender: str, msg: StockRequest):
    ctx.logger.info(f"Handling stock request {msg.request_id} from {sender}")
    try:
        items = filter_items(build_items(), msg.focus_items)
    except Exception as exc:
        ctx.logger.error(f"Error preparing snapshot: {exc}")
        items = []

    await ctx.send(sender, StockSnapshot(request_id=msg.request_id, items=items, synthetic=True))
    ctx.logger.info(f"StockSnapshot {msg.request_id} dispatched ({len(items)} items)")