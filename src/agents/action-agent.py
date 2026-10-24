import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

# ---- Agentverse Target Addresses ----
# Paste your deployed Inventory Agent and Simulation Agent addresses from Agentverse here:
INVENTORY_ADDR = os.getenv("INVENTORY_ADDR", "PASTE_INVENTORY_ADDRESS")
SIMULATION_ADDR = os.getenv("SIMULATION_ADDR", "PASTE_SIMULATION_ADDRESS")

# ---- Message Models ----
class StockRequest(Model):
    request_id: str
    focus_items: List[str] = []

class StockSnapshot(Model):
    request_id: str
    items: List[dict]
    synthetic: bool = True

class SimRequest(Model):
    request_id: str
    items: List[dict]
    scenario: dict

class SimResult(Model):
    request_id: str
    results: List[dict]

# ---- Agent & Protocol Setup ----
agent = Agent()

chat_proto = Protocol(spec=chat_protocol_spec)

SUPPLIERS_MAP = {
    "sup01": "SUP01", "apex": "SUP01", "apex pharma": "SUP01",
    "sup02": "SUP02", "lifeline": "SUP02", "lifeline biologics": "SUP02",
    "sup03": "SUP03", "himalaya": "SUP03", "himalaya medsupply": "SUP03",
    "sup04": "SUP04", "gaspro": "SUP04",
    "sup05": "SUP05", "citymed": "SUP05",
    "sup06": "SUP06", "safeguard": "SUP06",
    "sup07": "SUP07", "bharat": "SUP07", "bharat generics": "SUP07",
}

SUPPLIER_NAMES = {
    "SUP01": "Apex Pharma Distributors (Delhi)",
    "SUP02": "LifeLine Biologics (Noida)",
    "SUP03": "Himalaya MedSupply (Chandigarh)",
    "SUP04": "GasPro Industrial Gases (Ghaziabad)",
    "SUP05": "CityMed Wholesale (Delhi)",
    "SUP06": "SafeGuard Surgicals (Meerut)",
    "SUP07": "Bharat Generics (Gurugram)",
}

ITEMS_MAP = {
    "insulin": "I01", "glargine": "I01",
    "hepatitis": "I02", "vaccine": "I02",
    "oxytocin": "I03",
    "oxygen": "I04", "o2": "I04", "cylinder": "I04",
    "saline": "I05", "iv bag": "I05", "normal saline": "I05",
    "amoxicillin": "I06", "clav": "I06", "antibiotic": "I06",
    "ceftriaxone": "I07",
    "heparin": "I08",
    "gloves": "I09", "surgical gloves": "I09",
    "mask": "I10", "masks": "I10", "n95": "I10", "respirator": "I10",
    "cannula": "I11", "iv cannula": "I11",
    "paracetamol": "I12", "pcm": "I12"
}

def make_reply(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )

def parse_user_intent(text: str) -> dict:
    """Extract scenario parameters, preference changes, or approval signals from user text."""
    lower = text.lower().strip()

    # 1. Approval Check
    approval_tokens = ["approve", "approved", "confirm", "proceed", "yes, approve", "place order", "accept"]
    if any(lower == tok or lower.startswith(tok + " ") or lower.endswith(" " + tok) or lower == "yes" for tok in approval_tokens):
        # Check if user also expressed a preference in the same message, e.g.: "Yes, but I prefer LifeLine Biologics"
        pref_sup = None
        for key, sid in SUPPLIERS_MAP.items():
            if key in lower and key != "sup01":  # avoid matching baseline
                pref_sup = sid
                break
        return {"action": "approve", "preferred_supplier": pref_sup}

    # 2. Preference Only Change
    if "prefer" in lower or "preference" in lower or "budget" in lower:
        pref_sup = None
        for key, sid in SUPPLIERS_MAP.items():
            if key in lower:
                pref_sup = sid
                break
        budget_match = re.search(r"budget.*?(\d[\d,]*)", lower)
        budget = int(budget_match.group(1).replace(",", "")) if budget_match else None
        return {"action": "update_preference", "preferred_supplier": pref_sup, "budget_cap": budget}

    # 3. Preset Check
    if "sc1" in lower:
        return {"action": "simulate", "scenario": {"id": "SC1"}, "focus_item": "insulin"}
    if "sc2" in lower or "heatwave" in lower or "cold chain failure" in lower:
        return {"action": "simulate", "scenario": {"id": "SC2"}, "focus_item": "insulin"}
    if "sc3" in lower or "oxygen surge" in lower or "respiratory outbreak" in lower:
        return {"action": "simulate", "scenario": {"id": "SC3"}, "focus_item": "oxygen"}

    # 4. Status / Risk Brief Check
    if any(w in lower for w in ["risk brief", "today's risk", "todays risk", "status", "inventory overview", "report"]):
        return {"action": "simulate", "scenario": {"name": "Baseline Risk Assessment (No Active Disruption)"}, "focus_item": None}

    # 5. Dynamic Disruption Parsing
    supplier_id = None
    for key, sid in SUPPLIERS_MAP.items():
        if key in lower:
            supplier_id = sid
            break

    delay_days = 0
    delay_match = re.search(r"(?:delay(?:ed)?\s*(?:by\s*)?|(\d+)\s*days?\s*delay)(\d+)?\s*(?:days?)?", lower)
    if delay_match:
        val = delay_match.group(2) or delay_match.group(1)
        if val and val.isdigit():
            delay_days = int(val)

    demand_multiplier = {}
    focus_item = None
    for item_key, item_id in ITEMS_MAP.items():
        if item_key in lower:
            focus_item = item_key
            # Look for demand increase near this item
            pct_match = re.search(r"(?:up|increased?|surge|by)\s*(\d+)%", lower)
            if pct_match:
                pct = float(pct_match.group(1))
                demand_multiplier[item_id] = round(1.0 + pct / 100.0, 2)
            break

    # Spoilage / Cold chain
    spoilage = 0.0
    if "cold chain" in lower or "spoilage" in lower or "fridge" in lower:
        spoilage_match = re.search(r"(\d+)%\s*(?:spoilage|loss)", lower)
        spoilage = float(spoilage_match.group(1)) / 100.0 if spoilage_match else 0.25

    scenario = {
        "name": f"Disruption What-If: {supplier_id or 'Supplier'} delay {delay_days}d" if delay_days else "Custom What-If Scenario",
        "supplier_id": supplier_id,
        "delay_days": delay_days,
        "demand_multiplier": demand_multiplier,
        "spoilage_pct": spoilage
    }

    # Default to SC1 hero scenario if user mentioned SUP01 or insulin without explicit numbers
    if supplier_id == "SUP01" and (focus_item == "insulin" or "insulin" in lower) and delay_days == 0:
        return {"action": "simulate", "scenario": {"id": "SC1"}, "focus_item": "insulin"}

    return {"action": "simulate", "scenario": scenario, "focus_item": focus_item}

def format_purchase_order(rec: dict, hospital_name: str = "City General Hospital") -> str:
    """Generate a formal Purchase Order and ready-to-send supplier email draft."""
    po_num = f"PO-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:4].upper()}"
    sup_name = rec.get("supplier_name", rec.get("supplier_id", "Primary Supplier"))
    sup_id = rec.get("supplier_id", "SUP")
    item_name = rec.get("item_name", "Medical Supplies")
    qty = rec.get("qty", 0)
    cost = rec.get("total_cost_inr", 0)
    lead = rec.get("lead_days", 0)
    unit_cost = rec.get("unit_cost", round(cost / qty, 2) if qty else 0)

    order_lines = []
    if rec.get("type") == "bridge":
        order_lines.append(f"  • Supplier Order: {qty} units from {sup_name} ({lead} days lead time)")
        order_lines.append(f"  • Inter-Hospital Transfer: stock pulled from sister hospitals for immediate buffer")
    elif rec.get("type") == "expedited_order":
        order_lines.append(f"  • Expedited Supplier Order: {qty} units from {sup_name} (Urgent Priority, {lead} days)")
    elif rec.get("type") == "transfer":
        order_lines.append(f"  • Emergency Inter-Hospital Transfer: {qty} units")
    else:
        order_lines.append(f"  • Standard Replenishment: {qty} units from {sup_name} ({lead} days)")

    po_text = f"""✅ Purchase Order Approved & Prepared for Transmission

======================================================================
PURCHASE ORDER: {po_num}
ISSUING HOSPITAL: {hospital_name} (Greater Noida)
STATUS: AUTHORIZED - READY FOR DISPATCH
======================================================================
Vendor / Supplier: {sup_name} [ID: {sup_id}]
Item: {item_name}
Target Order Quantity: {qty:,} units
Committed Total (INR): ₹{cost:,}
Delivery SLA: {lead} days
Residual Stockout Risk: {int(rec.get('stockout_prob', 0) * 100)}%

Order Specifications:
{chr(10).join(order_lines)}
======================================================================

📧 READY-TO-SEND SUPPLIER DISPATCH EMAIL DRAFT:
----------------------------------------------------------------------
To: orders@{sup_id.lower()}.mednetwork.in
Cc: procurement@{hospital_name.lower().replace(' ', '')}.org
Subject: [URGENT] {po_num} Purchase Order Confirmation - {item_name}

Dear Logistics & Dispatch Team at {sup_name},

Please accept Purchase Order {po_num} from {hospital_name}.
We require delivery of {qty:,} units of {item_name} within {lead} days.

Specifications & Handling:
- Delivery Location: Central Hospital Receiving Dock, Gate 3
- Cold Chain: Strict 2°C - 8°C continuous data logger required (if applicable)
- Total PO Value: INR {cost:,} (Net 30 payment terms upon receipt verification)

Please confirm order receipt and send expected courier consignment tracking.

Sincerely,
MediTwin Automated Procurement Agent
{hospital_name} Supply Chain Management
----------------------------------------------------------------------"""
    return po_text

@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"MediTwin Action Agent active. Address: {ctx.agent.address}")
    ctx.logger.info(f"Orchestration targets -> Inventory: {INVENTORY_ADDR}, Simulation: {SIMULATION_ADDR}")

# ---- 1. User -> Action Agent (ASI:One Chat Protocol) ----
@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc),
        acknowledged_msg_id=msg.msg_id
    ))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    ctx.logger.info(f"User message received from {sender}: {text}")

    intent = parse_user_intent(text)
    user_key = f"user_pref_{sender}"
    prefs = ctx.storage.get(user_key) or {"preferred_suppliers": [], "budget_cap_inr": 600000}

    # Handle Preference Update
    if intent.get("preferred_supplier"):
        sup = intent["preferred_supplier"]
        if sup not in prefs["preferred_suppliers"]:
            prefs["preferred_suppliers"].insert(0, sup)
        ctx.storage.set(user_key, prefs)
    if intent.get("budget_cap"):
        prefs["budget_cap_inr"] = intent["budget_cap"]
        ctx.storage.set(user_key, prefs)

    # Handle Direct Approval
    if intent["action"] == "approve":
        last_rec = ctx.storage.get(f"pending_rec_{sender}")
        if last_rec:
            # If user preferred another supplier during approval, adjust recommendation
            if intent.get("preferred_supplier") and last_rec.get("options"):
                for opt in last_rec["options"]:
                    if opt.get("supplier_id") == intent["preferred_supplier"]:
                        last_rec = dict(opt)
                        last_rec["item_name"] = ctx.storage.get(f"pending_item_{sender}") or "Medical Item"
                        break
            reply_msg = format_purchase_order(last_rec)
            await ctx.send(sender, make_reply(reply_msg))
            return
        else:
            await ctx.send(sender, make_reply(
                "No pending recommendation to approve. Please describe a supply disruption (e.g., 'Supplier SUP01 is delayed 10 days and insulin demand is up 30%') to run a simulation."
            ))
            return

    # Trigger New Simulation Workflow (Action -> Inventory -> Simulation)
    rid = uuid4().hex[:8]
    ctx.storage.set(f"user_{rid}", sender)
    ctx.storage.set(f"text_{rid}", text)
    ctx.storage.set(f"scenario_{rid}", intent.get("scenario", {}))
    ctx.storage.set(f"focus_{rid}", intent.get("focus_item"))

    ctx.logger.info(f"Initiating request {rid}: Querying digital twin inventory")
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id=rid, focus_items=[intent.get("focus_item")] if intent.get("focus_item") else []))

@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass

# ---- 2. Inventory Agent -> Action Agent ----
@agent.on_message(model=StockSnapshot)
async def on_snapshot(ctx: Context, sender: str, msg: StockSnapshot):
    ctx.logger.info(f"Received StockSnapshot for {msg.request_id} with {len(msg.items)} genuine items")
    user_id = ctx.storage.get(f"user_{msg.request_id}")
    prefs = ctx.storage.get(f"user_pref_{user_id}") if user_id else {}
    scenario = ctx.storage.get(f"scenario_{msg.request_id}") or {}
    focus = ctx.storage.get(f"focus_{msg.request_id}")

    # Build structured simulation payload
    sim_scenario = dict(scenario)
    if focus:
        sim_scenario["item"] = focus
    if prefs:
        sim_scenario["preferences"] = prefs

    ctx.logger.info(f"Forwarding real inventory and scenario to Simulation Agent: {sim_scenario}")
    await ctx.send(SIMULATION_ADDR, SimRequest(
        request_id=msg.request_id,
        items=msg.items,
        scenario=sim_scenario
    ))

# ---- 3. Simulation Agent -> Action Agent -> User ----
@agent.on_message(model=SimResult)
async def on_result(ctx: Context, sender: str, msg: SimResult):
    ctx.logger.info(f"Received SimResult {msg.request_id}")
    user = ctx.storage.get(f"user_{msg.request_id}")
    if not user:
        ctx.logger.error(f"No active user mapped to request {msg.request_id}")
        return

    # Extract meta and item records
    meta = {}
    items = []
    for r in msg.results:
        if "meta" in r:
            meta = r["meta"]
        elif "item" in r:
            items.append(r)

    if not items and not meta:
        err = msg.results[0].get("error", "Unknown simulation failure") if msg.results else "No simulation data returned."
        await ctx.send(user, make_reply(f"Simulation warning: {err}"))
        return

    # Extract Key Findings
    scenario_info = meta.get("scenario", {})
    summary = meta.get("summary", {"critical": 0, "high": 0, "medium": 0, "low": 0})
    focus = meta.get("focus") or (items[0] if items else None)

    lines = []
    lines.append(f"📊 **MediTwin Digital Twin Risk Report**")
    lines.append(f"Scenario: *{scenario_info.get('name', 'Custom Disruption Analysis')}*")
    lines.append(f"Hospital Risk Profile: **{summary.get('critical', 0)} Critical**, **{summary.get('high', 0)} High**, **{summary.get('medium', 0)} Medium**, **{summary.get('low', 0)} Low**\n")

    # Focus Item Detailed Analytics
    if focus:
        f_name = focus.get("item", focus.get("item_id", "Primary Item"))
        cover = focus.get("days_of_cover", 0.0)
        prob = int(round(focus.get("stockout_prob", 0.0) * 100))
        severity = focus.get("severity", "medium").upper()
        lead = focus.get("primary_lead_days", 0)
        shortfall = focus.get("shortfall_days", 0)

        lines.append(f"⚠️ **Focus Disruption: {f_name}**")
        lines.append(f"• Current Cover: **{cover:.1f} days** | Delayed Lead Time: **{lead} days**")
        lines.append(f"• Stockout Probability: **{prob}%** (Severity: **{severity}**)")
        if shortfall > 0:
            lines.append(f"• Projected Stockout Gap: **{shortfall:.1f} days** before primary supplier arrives.\n")

        # Mitigation Plans Evaluated
        options = focus.get("options", [])
        recommended_id = focus.get("recommended")
        rec_option = None

        if options:
            lines.append(f"💡 **Simulated Mitigation Options (Monte Carlo Evaluated):**")
            for i, opt in enumerate(options[:4], 1):
                is_rec = opt["id"] == recommended_id
                tag = " ⭐ **[RECOMMENDED]**" if is_rec else ""
                sup_name = SUPPLIER_NAMES.get(opt.get("supplier_id"), opt.get("label"))
                lines.append(f" {i}. **{opt.get('label')}**{tag}")
                lines.append(f"    - Lead Time: {opt.get('lead_days')}d | Total Cost: ₹{opt.get('total_cost_inr', 0):,} | Residual Risk: {int(opt.get('stockout_prob', 0)*100)}%")
                if is_rec:
                    rec_option = opt

            if focus.get("recommendation_reason"):
                lines.append(f"\n*Recommendation Rationale:* {focus.get('recommendation_reason')}")

            # Store the best option for instant approval
            if rec_option:
                rec_option["item_name"] = f_name
                rec_option["options"] = options
                ctx.storage.set(f"pending_rec_{user}", rec_option)
                ctx.storage.set(f"pending_item_{user}", f_name)

    # Cascade Detection: Check other affected items
    cascade_items = [it for it in items if it != focus and it.get("risk_score", 0) > 5.0]
    if cascade_items:
        lines.append(f"\n🔄 **Cascade Disruption Alert:**")
        for ci in cascade_items[:2]:
            lines.append(f"• **{ci.get('item')}**: Cover {ci.get('days_of_cover'):.1f}d, Stockout Risk {int(ci.get('stockout_prob', 0)*100)}% (shares affected supplier)")

    lines.append(f"\n👉 **Next Action:**")
    lines.append(f"Reply **'Approve'** to generate the Purchase Order & Supplier Dispatch Email, or customize (e.g. *'I prefer LifeLine Biologics'*).")

    report_text = "\n".join(lines)
    await ctx.send(user, make_reply(report_text))
    ctx.logger.info(f"Delivered full logistics report to user {user}")

agent.include(chat_proto, publish_manifest=True)

if __name__ == "__main__":
    agent.run()