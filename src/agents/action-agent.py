"""MediTwin Action Agent: ASI:One front door + orchestrator.
user text -> ASI:One LLM intent extraction (keyword fallback) -> Inventory Agent (stock twin)
-> Simulation Agent (Monte Carlo what-if) -> ranked fixes -> approval -> purchase order + supplier email.
Synthetic data; logistics decision support, not clinical advice."""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List
from uuid import uuid4

from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

INVENTORY_ADDR = "PASTE_INVENTORY_AGENT_ADDRESS"
SIMULATION_ADDR = "PASTE_SIMULATION_AGENT_ADDRESS"
INV_TIMEOUT = 15   # seconds before falling back to the Simulation Agent's embedded snapshot
SIM_TIMEOUT = 40   # seconds before telling the user the simulation is unavailable
TRANSFER_COST_INR = 1500
IST = timezone(timedelta(hours=5, minutes=30))


# ---- message models (must match the other two agents exactly) ----
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


SUPPLIERS = {
    "SUP01": "Apex Pharma Distributors", "SUP02": "LifeLine Biologics", "SUP03": "Himalaya MedSupply",
    "SUP04": "GasPro Industrial Gases", "SUP05": "CityMed Wholesale", "SUP06": "SafeGuard Surgicals",
    "SUP07": "Bharat Generics",
}
ITEM_WORDS = ["insulin", "hepatitis", "vaccine", "oxytocin", "oxygen", "adrenaline", "epinephrine",
              "ceftriaxone", "saline", "heparin", "gloves", "n95", "mask", "cannula", "paracetamol"]
ITEM_ALIASES = {"epinephrine": "adrenaline", "vaccine": "hepatitis", "mask": "n95"}
DEFAULT_PREFS = {"preferred_suppliers": [], "budget_cap_inr": 600000}

SYSTEM_PROMPT = """You convert a hospital supply-chain manager's message into JSON for a simulation engine.
Reply with ONE JSON object only, no markdown, no prose:
{"intent": "simulate" | "brief" | "set_preference" | "help",
 "scenario": null | {"id": "SC1"|"SC2"|"SC3"} | {"supplier_id": "SUP0x", "delay_days": int, "demand_multiplier": {"<item name>": float}, "spoilage_pct": float},
 "item": "<item the user cares about, or null>",
 "preferences": {"preferred_suppliers": ["SUP0x"], "budget_cap_inr": int}}
Rules:
- what-if / disruption / delay / surge / shortage questions -> "simulate". Include only the scenario fields the user stated.
- "+30% demand" -> 1.3. A demand change with no item -> "demand_multiplier": <float>. Heatwave or fridge failure -> "spoilage_pct" (default 0.25).
- status, today's risks, risk brief, morning brief -> "brief" with scenario null.
- naming a preferred supplier or a budget -> put it in "preferences" (only fields stated); with no what-if, intent is "set_preference". 1 lakh = 100000.
- greetings or unclear -> "help".
Presets: SC1 = SUP01 delayed 10 days + insulin demand +30%; SC2 = heatwave cold-chain failure (25% spoilage); SC3 = respiratory outbreak (oxygen x1.6, N95 x1.5).
Items: insulin, hepatitis B vaccine, oxytocin, oxygen cylinder, adrenaline, ceftriaxone, normal saline, heparin, surgical gloves, N95 mask, IV cannula, paracetamol.
Suppliers: """ + ", ".join(f"{k} = {v}" for k, v in SUPPLIERS.items())

HELP = ("I'm **MediTwin**, a live digital twin of City General Hospital's medical supply chain. "
        "I simulate disruptions, predict stockouts and draft the fix for your approval.\n\nTry:\n"
        "- Give me today's risk brief\n"
        "- What if SUP01 is delayed 10 days and insulin demand rises 30%?\n"
        "- Run scenario SC3\n"
        "- Prefer LifeLine Biologics and keep my budget under 5 lakh")
FOOTER = "_Synthetic data. Logistics decision support, not clinical advice._"

agent = Agent()
chat_proto = Protocol(spec=chat_protocol_spec)


# ---------------- helpers ----------------
def make_reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text)])


async def reply(ctx: Context, to: str, text: str):
    await ctx.send(to, make_reply(text))


def pct(p) -> str:
    return f"{round((p or 0) * 100)}%"


def inr(x) -> str:
    return f"Rs {int(round(x or 0)):,}"


def get_prefs(ctx: Context, user: str) -> dict:
    return {**DEFAULT_PREFS, **(ctx.storage.get(f"prefs_{user}") or {})}


def clean_scenario(sc):
    if not isinstance(sc, dict) or not sc:
        return None
    if str(sc.get("id", "")).upper() in ("SC1", "SC2", "SC3"):
        return {"id": str(sc["id"]).upper()}
    out = {k: sc[k] for k in ("supplier_id", "delay_days", "demand_multiplier", "spoilage_pct")
           if sc.get(k) not in (None, "", {}, 0)}
    if "supplier_id" in out:
        out["supplier_id"] = str(out["supplier_id"]).upper()
    return out or None


def clean_prefs(p):
    out = {}
    if isinstance(p, dict):
        ids = [str(s).upper() for s in (p.get("preferred_suppliers") or []) if str(s).upper() in SUPPLIERS]
        if ids:
            out["preferred_suppliers"] = ids
        try:
            if p.get("budget_cap_inr") and int(p["budget_cap_inr"]) > 0:
                out["budget_cap_inr"] = int(p["budget_cap_inr"])
        except (TypeError, ValueError):
            pass
    return out


# ---------------- intent extraction ----------------
def _asi1_sync(text: str) -> str:
    from openai import OpenAI  # ASI:One exposes an OpenAI-compatible API
    client = OpenAI(base_url=os.getenv("ASI1_BASE_URL", "https://api.asi1.ai/v1"),
                    api_key=os.getenv("ASI1_API_KEY", ""))
    r = client.chat.completions.create(
        model="asi1", temperature=0,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}])
    return r.choices[0].message.content or ""


def keyword_parse(text: str) -> dict:
    t = text.lower()
    item = next((w for w in ITEM_WORDS if w in t), None)
    item = ITEM_ALIASES.get(item, item)
    prefs = {}
    b = re.search(r"budget[^\d]*(\d[\d,.]*)\s*(lakh|lac|k)?", t)
    if b:
        v = float(b.group(1).replace(",", ""))
        prefs["budget_cap_inr"] = int(v * {"lakh": 100000, "lac": 100000, "k": 1000}.get(b.group(2), 1))
    if "prefer" in t:
        ids = [sid for sid, n in SUPPLIERS.items() if sid.lower() in t or n.lower().split()[0] in t]
        if ids:
            prefs["preferred_suppliers"] = ids
    preset = re.search(r"\bsc\s?([123])\b", t)
    sup = re.search(r"\bsup\s?0?([1-7])\b", t)
    days = re.search(r"(\d+)\s*day", t)
    surge = re.search(r"([+-]?\d+)\s*%", t)
    if preset:
        return {"intent": "simulate", "scenario": {"id": "SC" + preset.group(1)}, "item": item, "preferences": prefs}
    if sup or (days and "delay" in t) or surge or "heatwave" in t:
        sc = {}
        if sup:
            sc["supplier_id"] = "SUP0" + sup.group(1)
        if days:
            sc["delay_days"] = int(days.group(1))
        if surge:
            m = 1 + int(surge.group(1)) / 100.0
            sc["demand_multiplier"] = {item: m} if item else m
        if "heatwave" in t or "fridge" in t:
            sc["spoilage_pct"] = 0.25
        return {"intent": "simulate", "scenario": sc, "item": item, "preferences": prefs}
    if prefs:
        return {"intent": "set_preference", "preferences": prefs}
    if any(w in t for w in ("brief", "risk", "status", "today")):
        return {"intent": "brief"}
    if item:
        return {"intent": "simulate", "scenario": None, "item": item}
    return {"intent": "help"}


async def parse_intent(ctx: Context, text: str):
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_asi1_sync, text), timeout=20)
        j = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        if j.get("intent") in ("simulate", "brief", "set_preference", "help"):
            return j, "asi1"
    except Exception as e:
        ctx.logger.warning(f"ASI:One parse failed, using keyword fallback: {e}")
    return keyword_parse(text), "keyword-fallback"


# ---------------- orchestration ----------------
def _open(ctx: Context) -> list:
    return list(ctx.storage.get("open_rids") or [])


def close_req(ctx: Context, rid: str):
    ctx.storage.set("open_rids", [r for r in _open(ctx) if r != rid])
    ctx.storage.set(f"req_{rid}", None)


async def start_run(ctx, user, scenario, item, prefs, kind, intro=""):
    rid = uuid4().hex[:8]
    ctx.storage.set(f"req_{rid}", {"user": user, "scenario": scenario, "item": item, "prefs": prefs,
                                   "kind": kind, "intro": intro, "notes": [], "stage": "inventory",
                                   "t": time.time()})
    ctx.storage.set("open_rids", _open(ctx) + [rid])
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id=rid, focus_items=[]))
    ctx.logger.info(f"[{rid}] -> Inventory | kind={kind} scenario={scenario} item={item}")


async def send_sim(ctx, rid, req, items):
    sc = dict(req.get("scenario") or {})
    if req.get("item"):
        sc["item"] = req["item"]
    sc["preferences"] = req.get("prefs") or {}
    req.update(stage="simulation", t=time.time())
    ctx.storage.set(f"req_{rid}", req)
    await ctx.send(SIMULATION_ADDR, SimRequest(request_id=rid, items=items, scenario=sc))
    ctx.logger.info(f"[{rid}] -> Simulation with {len(items)} items")


def format_result(req, items, meta):
    s, sc, f = meta.get("summary", {}), meta.get("scenario", {}), meta.get("focus")
    head = {"brief": "Today's risk brief", "rerun": "Re-run with your updated preferences"}.get(
        req["kind"], "Scenario: " + sc.get("name", "custom"))
    L = [req["intro"]] if req.get("intro") else []
    L += [f"**{head}**",
          f"Risk overview: {s.get('critical', 0)} critical, {s.get('high', 0)} high, "
          f"{s.get('medium', 0)} medium, {s.get('low', 0)} low", ""]
    for r in items[:3]:
        L.append(f"- {r['item']}: {r['days_of_cover']} days of cover, stockout risk "
                 f"{pct(r['stockout_prob'])} ({r['severity']})")
    pending = None
    if f and f.get("options"):
        rec, allopts = f.get("recommended"), f["options"]
        opts = allopts[:4]
        if rec and all(o["id"] != rec for o in opts):
            opts = [o for o in allopts if o["id"] == rec] + allopts[:3]
        L += ["", f"**Fix options for {f['item']}** (current plan: stockout risk {pct(f['stockout_prob'])}, "
                  f"first stockout ~day {f.get('expected_first_stockout_day') or '-'})"]
        for i, o in enumerate(opts, 1):
            L.append(f"{i}. {o['label']} | risk {pct(o['stockout_prob'])} | {inr(o['total_cost_inr'])}"
                     f"{'' if o['within_budget'] else ' (over budget)'}"
                     f"{'  **<- recommended**' if o['id'] == rec else ''}")
        if f.get("recommendation_reason"):
            L.append("Why: " + f["recommendation_reason"])
        L += ["", "Reply **YES** to approve the recommended plan, **option N** to pick another, or **NO** to cancel."]
        pending = {"item": f["item"], "item_id": f.get("item_id"), "options": opts, "recommended": rec,
                   "scenario": sc.get("name", "the current disruption")}
    elif f and f.get("note"):
        L += ["", f"{f['item']}: {f['note']}"]
    elif req["kind"] == "brief":
        L += ["", "No item needs action today. Ask a what-if, e.g. *What if SUP01 is delayed 10 days "
                  "and insulin demand rises 30%?*"]
    notes = req.get("notes", []) + meta.get("warnings", [])
    if "embedded" in str(meta.get("data_source", "")) and not req.get("notes"):
        notes.append("Used the Simulation Agent's stored snapshot (inventory data unavailable).")
    L += [""] + [f"_Note: {n}_" for n in notes] + [FOOTER]
    return "\n".join(L), pending


def make_po(p, o):
    now = datetime.now(IST)
    po_no = f"PO-{now:%Y%m%d}-{uuid4().hex[:4].upper()}"
    transfers = o.get("transfers") or []
    sid = o.get("supplier_id")
    order_qty = o.get("order_qty", o.get("qty", 0) if sid else 0)
    L = [f"**Approved: {o['label']}**", ""]
    if sid and order_qty:
        sup = SUPPLIERS.get(sid, sid)
        supplier_cost = o["total_cost_inr"] - TRANSFER_COST_INR * len(transfers)
        unit = supplier_cost / order_qty
        exp = o.get("type") == "expedited_order"
        L += ["**Purchase order**", "```",
              f"PO number : {po_no}", f"Date      : {now:%d %b %Y, %H:%M} IST",
              "Buyer     : City General Hospital, Greater Noida",
              f"Supplier  : {sup} ({sid})", f"Item      : {p['item']}",
              f"Quantity  : {order_qty:,}", f"Unit price: Rs {unit:,.2f}",
              f"Total     : {inr(supplier_cost)}",
              f"Delivery  : within {o['lead_days']} days{' (EXPEDITED)' if exp else ''}",
              "Status    : Approved, awaiting supplier confirmation", "```", "",
              f"**Email to {sup}**",
              f"Subject: {po_no} | {p['item']} x {order_qty:,}{' | URGENT' if exp else ''}", "",
              f"Dear {sup} team,", "",
              f"Please find attached purchase order {po_no} for {order_qty:,} units of {p['item']} at "
              f"Rs {unit:,.2f} per unit (total {inr(supplier_cost)}). Because of {p['scenario'].lower()}, "
              f"we need delivery within {o['lead_days']} days. Please confirm the dispatch date and any "
              f"cold-chain handling requirements by reply.", "",
              "Regards,", "Procurement Team, City General Hospital", ""]
    for t in transfers:
        L.append(f"**Transfer request to {t['name']}**: please send {t['qty']:,} units of {p['item']} today "
                 f"(est. logistics {inr(TRANSFER_COST_INR)}).")
    if not transfers and o.get("type") in ("transfer", "bridge"):
        L.append("**Transfer request** sent to sister hospitals for their spare units.")
    L += ["", f"Total plan cost: {inr(o['total_cost_inr'])}. Expected stockout risk after this plan: "
              f"{pct(o['stockout_prob'])}.", FOOTER]
    return po_no, "\n".join(L)


# ---------------- handlers ----------------
@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Action Agent up. Address: {ctx.agent.address}")


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                               acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    ctx.logger.info(f"User {sender[:16]}...: {text}")
    t = text.lower()

    # 1. pending approval (deterministic, no LLM needed)
    pending = ctx.storage.get(f"pending_{sender}")
    if pending:
        opt = re.search(r"option\s*#?\s*(\d+)", t)
        if opt or re.match(r"\s*(yes|y|approve|approved|confirm|go ahead|ok|okay)\b", t):
            opts = pending["options"]
            if opt:
                i = int(opt.group(1)) - 1
                if not 0 <= i < len(opts):
                    await reply(ctx, sender, f"Please choose option 1 to {len(opts)}.")
                    return
                o = opts[i]
            else:
                o = next((x for x in opts if x["id"] == pending.get("recommended")), opts[0])
            po_no, doc = make_po(pending, o)
            hist = (ctx.storage.get(f"orders_{sender}") or [])[-9:]
            ctx.storage.set(f"orders_{sender}", hist + [{"po": po_no, "item": pending["item"],
                                                        "plan": o["label"], "cost": o["total_cost_inr"]}])
            ctx.storage.set(f"pending_{sender}", None)
            await reply(ctx, sender, doc)
            return
        if re.match(r"\s*(no|cancel|reject|stop)\b", t):
            ctx.storage.set(f"pending_{sender}", None)
            await reply(ctx, sender, "Cancelled. No order was placed.")
            return

    # 2. intent extraction with ASI:One LLM (keyword fallback)
    intent, src = await parse_intent(ctx, text)
    ctx.logger.info(f"Intent via {src}: {intent}")
    kind = intent.get("intent")
    new_prefs = clean_prefs(intent.get("preferences"))
    intro = ""
    if new_prefs:
        saved = {**(ctx.storage.get(f"prefs_{sender}") or {}), **new_prefs}
        ctx.storage.set(f"prefs_{sender}", saved)
        bits = []
        if saved.get("preferred_suppliers"):
            bits.append("preferred suppliers " + ", ".join(SUPPLIERS[s] for s in saved["preferred_suppliers"]))
        if saved.get("budget_cap_inr"):
            bits.append("budget cap " + inr(saved["budget_cap_inr"]))
        intro = "Saved your preferences: " + "; ".join(bits) + ".\n"
    prefs = get_prefs(ctx, sender)

    if kind == "set_preference":
        last = ctx.storage.get(f"last_{sender}")
        if not new_prefs:
            await reply(ctx, sender, "I couldn't find a supplier or budget in that. " + HELP)
        elif last:
            await start_run(ctx, sender, last.get("scenario"), last.get("item"), prefs, "rerun", intro)
        else:
            await reply(ctx, sender, intro + "I'll apply these to every simulation from now on.")
        return
    if kind == "brief":
        await start_run(ctx, sender, None, None, prefs, "brief", intro)
        return
    if kind == "simulate":
        await start_run(ctx, sender, clean_scenario(intent.get("scenario")), intent.get("item"),
                        prefs, "simulate", intro)
        return
    await reply(ctx, sender, intro + HELP)


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


@agent.on_message(model=StockSnapshot)
async def on_snapshot(ctx: Context, sender: str, msg: StockSnapshot):
    req = ctx.storage.get(f"req_{msg.request_id}")
    if not req or req.get("stage") != "inventory":
        ctx.logger.info(f"[{msg.request_id}] late/unknown StockSnapshot ignored")
        return
    ctx.logger.info(f"[{msg.request_id}] <- Inventory: {len(msg.items)} items")
    await send_sim(ctx, msg.request_id, req, msg.items)


@agent.on_message(model=SimResult)
async def on_result(ctx: Context, sender: str, msg: SimResult):
    rid = msg.request_id
    req = ctx.storage.get(f"req_{rid}")
    if not req or req.get("stage") != "simulation":
        ctx.logger.info(f"[{rid}] late/unknown SimResult ignored")
        return
    user = req["user"]
    close_req(ctx, rid)
    if not msg.results or "error" in msg.results[0]:
        err = msg.results[0].get("error") if msg.results else "empty result"
        await reply(ctx, user, f"The simulation could not run ({err}). Please rephrase or try again.")
        return
    meta = next((r["meta"] for r in msg.results if "meta" in r), {})
    items = [r for r in msg.results if "item_id" in r]
    text, pending = format_result(req, items, meta)
    ctx.storage.set(f"pending_{user}", pending)
    if req["kind"] != "brief":
        ctx.storage.set(f"last_{user}", {"scenario": req.get("scenario"), "item": req.get("item")})
    ctx.logger.info(f"[{rid}] <- Simulation, replying to user")
    await reply(ctx, user, text)


@agent.on_interval(period=5.0)
async def watchdog(ctx: Context):
    """Error recovery: Inventory silent -> simulate on stored snapshot; Simulation silent -> tell the user."""
    for rid in _open(ctx):
        req = ctx.storage.get(f"req_{rid}")
        if not req:
            close_req(ctx, rid)
            continue
        age = time.time() - req.get("t", 0)
        if req["stage"] == "inventory" and age > INV_TIMEOUT:
            ctx.logger.warning(f"[{rid}] Inventory timeout, falling back")
            req["notes"] = req.get("notes", []) + [
                "Inventory Agent did not respond; used the Simulation Agent's last known snapshot."]
            await send_sim(ctx, rid, req, [])
        elif req["stage"] == "simulation" and age > SIM_TIMEOUT:
            ctx.logger.warning(f"[{rid}] Simulation timeout")
            close_req(ctx, rid)
            await reply(ctx, req["user"], "Sorry, the Simulation Agent is not responding right now. "
                                          "Please try again in a minute.")


agent.include(chat_proto, publish_manifest=True)