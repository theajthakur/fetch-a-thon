import json
import os
from typing import List
from uagents import Agent, Context, Model

# ---- Message Models (shared across MediTwin agents) ----
class StockRequest(Model):
    request_id: str
    focus_items: List[str] = []

class StockSnapshot(Model):
    request_id: str
    items: List[dict]
    synthetic: bool = True

agent = Agent()

# Embedded Digital Twin Hospital State (ensures 100% self-contained deployment on Agentverse)
DATA_JSON = r'''
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
                    "id": "SUP02",
                    "lead_days": 4,
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
            "demand_std": 3,
            "reorder_level": 80,
            "expiry_days": 1800,
            "suppliers": [
                {
                    "id": "SUP04",
                    "lead_days": 2,
                    "unit_cost": 650
                },
                {
                    "id": "SUP05",
                    "lead_days": 3,
                    "unit_cost": 680
                }
            ]
        },
        {
            "id": "I05",
            "name": "Normal Saline 0.9% 500mL IV bag",
            "unit": "bag",
            "criticality": 4,
            "cold_chain": false,
            "stock": 1500,
            "daily_demand": 90,
            "demand_std": 15,
            "reorder_level": 900,
            "expiry_days": 365,
            "suppliers": [
                {
                    "id": "SUP05",
                    "lead_days": 3,
                    "unit_cost": 32
                },
                {
                    "id": "SUP07",
                    "lead_days": 4,
                    "unit_cost": 30
                }
            ]
        },
        {
            "id": "I06",
            "name": "Amoxicillin-Clavulanic Acid 625mg",
            "unit": "strip",
            "criticality": 3,
            "cold_chain": false,
            "stock": 2200,
            "daily_demand": 110,
            "demand_std": 20,
            "reorder_level": 1100,
            "expiry_days": 400,
            "suppliers": [
                {
                    "id": "SUP07",
                    "lead_days": 4,
                    "unit_cost": 85
                },
                {
                    "id": "SUP05",
                    "lead_days": 3,
                    "unit_cost": 90
                }
            ]
        },
        {
            "id": "I07",
            "name": "Ceftriaxone 1g injection",
            "unit": "vial",
            "criticality": 4,
            "cold_chain": false,
            "stock": 1800,
            "daily_demand": 80,
            "demand_std": 18,
            "reorder_level": 900,
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
    ]
}
'''
EMBEDDED_DATA = json.loads(DATA_JSON)

def load_data():
    """Load hospital digital twin state from data.json if available, or use embedded data."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "dataset", "data.json"),
        os.path.join(os.path.dirname(__file__), "dataset", "data.json"),
        "dataset/data.json",
        "data.json"
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return EMBEDDED_DATA

@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Inventory Agent up. Address: {ctx.agent.address}")
    data = load_data()
    ctx.logger.info(f"Digital Twin active with {len(data.get('items', []))} catalog items.")

@agent.on_message(model=StockRequest)
async def on_stock_request(ctx: Context, sender: str, msg: StockRequest):
    ctx.logger.info(f"StockRequest {msg.request_id} received from {sender}")
    data = load_data()
    items = data.get("items", [])
    ctx.logger.info(f"Returning genuine digital twin stock ({len(items)} items) for request {msg.request_id}")
    await ctx.send(sender, StockSnapshot(request_id=msg.request_id, items=items, synthetic=True))
    ctx.logger.info(f"Replied with StockSnapshot {msg.request_id}")

if __name__ == "__main__":
    agent.run()