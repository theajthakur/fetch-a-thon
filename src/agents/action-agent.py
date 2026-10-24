"""MediTwin Action Agent: ASI:One front door + orchestrator.
user text -> ASI:One LLM intent extraction (keyword fallback) -> Inventory Agent (stock twin)
-> Simulation Agent (Monte Carlo what-if) -> ranked fixes -> approval -> purchase order + supplier email.
Synthetic data; logistics decision support, not clinical advice."""
import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import List
from uuid import uuid4

from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)
try:  # ASI:One closes the turn and renders the answer when the reply ends the session
    from uagents_core.contrib.protocols.chat import EndSessionContent
except ImportError:
    EndSessionContent = None
try:  # Agentverse interactive cards travel in a MetadataContent block (dict[str, str])
    from uagents_core.contrib.protocols.chat import MetadataContent
except ImportError:
    MetadataContent = None

INVENTORY_ADDR = "PASTE_INVENTORY_AGENT_ADDRESS"
SIMULATION_ADDR = "PASTE_SIMULATION_AGENT_ADDRESS"
INV_TIMEOUT = 15   # seconds before falling back to the Simulation Agent's embedded snapshot
SIM_TIMEOUT = 40   # seconds before telling the user the simulation is unavailable
TRANSFER_COST_INR = 1500
LLM_TIMEOUT = 8        # seconds; keyword fallback after this
LLM_COOLDOWN = 600     # seconds to skip the LLM after a quota/auth error (saves ~1s per message)
SYNC_PERIOD = 60.0     # Action Agent pulls a fresh snapshot from the Inventory Agent this often
CACHE_MAX_AGE = 300    # seconds a synced snapshot may be used directly (removes one hop from each request)
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
{"intent": "simulate" | "brief" | "set_preference" | "help" | "none",
 "scenario": null | {"id": "SC1"|"SC2"|"SC3"} | {"supplier_id": "SUP0x", "delay_days": int, "demand_multiplier": {"<item name>": float}, "spoilage_pct": float},
 "item": "<item the user cares about, or null>",
 "preferences": {"preferred_suppliers": ["SUP0x"], "budget_cap_inr": int}}
Rules:
- what-if / disruption / delay / surge / shortage questions -> "simulate". Include only the scenario fields the user stated.
- "+30% demand" -> 1.3. A demand change with no item -> "demand_multiplier": <float>. Heatwave or fridge failure -> "spoilage_pct" (default 0.25).
- status, today's risks, risk brief, morning brief -> "brief" with scenario null.
- naming a preferred supplier or a budget -> put it in "preferences" (only fields stated); with no what-if, intent is "set_preference". 1 lakh = 100000.
- greetings or unclear questions -> "help".
- "none" when the message is commentary, a summary of earlier results, an acknowledgement or advice (e.g. "budget is now set", "nothing to do", "the scenario is in good shape") rather than a NEW instruction. Only use "set_preference" when the message tells you to set or change a supplier preference or budget.
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

# Hosted-agent storage calls are slow (~2s each in the logs), so short-lived, non-critical state lives in memory.
MEM = {"seen": [], "lasttext": {}, "llm_off_until": 0.0, "inv_cache": None}

agent = Agent()
chat_proto = Protocol(spec=chat_protocol_spec)


# ---------------- helpers ----------------
def make_reply(text: str, card: dict = None) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if card and MetadataContent is not None:
        content.append(MetadataContent(type="metadata", metadata=card))  # card needs the session open
    elif EndSessionContent is not None:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


async def reply(ctx: Context, to: str, text: str, card: dict = None):
    ctx.logger.info(f"Reply to {to[:16]}... ({len(text)} chars, card={'yes' if card else 'no'}): {text[:120]!r}")
    await ctx.send(to, make_reply(text, card))


# ---------------- interactive approval card ----------------
CARDS_ENABLED = True
CARD_KIND = "detail"     # summary rows + radio list of plans + Approve / Cancel buttons


def card_selection(i, o):
    """Identifiers every CTA carries; they come back as JSON (direct @mention) or in planner prose."""
    return {"action": "approve_plan", "option": str(i), "plan_id": o["id"]}


def build_card_payload(pending):
    """'detail' card: what is at risk, every fix plan as a radio choice (recommended first), Approve/Cancel."""
    opts, rec = pending.get("options") or [], pending.get("recommended")
    if not opts:
        return None
    ordered = sorted(opts, key=lambda o: o["id"] != rec)
    rec_opt = next((o for o in opts if o["id"] == rec), None)
    rows = [{"label": "Item", "value": pending["item"]},
            {"label": "Scenario", "value": pending.get("scenario", "-")}]
    if pending.get("risk_now") is not None:
        rows.append({"label": "Stockout risk if no action", "value": pct(pending["risk_now"])})
    if rec_opt:
        rows.append({"label": "Recommended", "value": f"{rec_opt['label']} ({inr(rec_opt['total_cost_inr'])})"})
    choices = [{"value": o["id"],
                "label": ("Recommended: " if o["id"] == rec else "") + o["label"],
                "secondary_text": f"Risk {pct(o['stockout_prob'])} | {inr(o['total_cost_inr'])} | {o['lead_days']}d"
                                  + ("" if o.get("within_budget", True) else " | over budget")}
               for o in ordered]
    return {"title": f"Fix plan for {pending['item']}",
            "summary_rows": rows,
            "sub_options": {"name": "plan_id", "kind": "radio", "label": "Choose a plan", "choices": choices},
            "ctas": [{"label": "Approve & draft PO", "selection": {"action": "approve_plan"}, "primary": True},
                     {"label": "Cancel", "selection": {"action": "cancel_plan"}}]}


def build_card(pending):
    if not (CARDS_ENABLED and pending):
        return None
    try:
        payload = build_card_payload(pending)
        if not payload:
            return None
        return {"card_protocol_version": "1", "requires_card_interaction": "true", "card_kind": CARD_KIND,
                "card_payload": json.dumps(payload), "preferred_drawer_width_px": "540"}
    except Exception:
        return None  # never let a card problem block the text answer


def parse_selection(text, pending):
    """Card click -> option number, "cancel", or None. Tries JSON first, then planner prose."""
    opts = pending.get("options") or []
    try:
        j = json.loads(text)
        if isinstance(j, dict):
            act = str(j.get("action", ""))
            if act == "cancel_plan":
                return "cancel"
            if act == "approve_plan":
                for i, o in enumerate(opts, 1):
                    if o["id"] == j.get("plan_id"):
                        return i
                if str(j.get("option", "")).isdigit():
                    return int(j["option"])
                return "recommended"  # Approve clicked without picking a radio option
    except (ValueError, TypeError):
        pass
    low = text.lower()
    if "cancel_plan" in low:
        return "cancel"
    for i, o in enumerate(opts, 1):
        if o["id"].lower() in low:
            return i
    return None


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
            if p.get("budget_cap_inr") and int(p["budget_cap_inr"]) >= 10000:
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


def _budget(t):
    for pat in (r"budget\D{0,25}?(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|k)?\b",
                r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|k)\b[^.]{0,20}budget"):
        m = re.search(pat, t)
        if m:
            v = float(m.group(1).replace(",", "")) * {"lakh": 1e5, "lac": 1e5, "k": 1e3}.get(m.group(2) or "", 1)
            if v >= 10000:
                return int(v)
    return None


def _named_suppliers(t):
    return [sid for sid, n in SUPPLIERS.items()
            if re.search(r"\b" + sid.lower() + r"\b", t) or n.lower().split()[0] in t]


def keyword_parse(text: str) -> dict:
    """Strict fallback parser: only acts on explicit phrasing, so paraphrased chatter can't trigger
    bogus scenarios (e.g. '5.4 days of cover' is not a delay, '1% risk' is not a demand surge)."""
    t = text.lower()
    item = next((w for w in ITEM_WORDS if w in t), None)
    item = ITEM_ALIASES.get(item, item)
    prefs = {}
    b = _budget(t)
    if b:
        prefs["budget_cap_inr"] = b
    if re.search(r"\bprefer(?:red)?\b|\bswitch to\b", t):
        ids = _named_suppliers(t)
        if ids:
            prefs["preferred_suppliers"] = ids
    preset = re.search(r"\bsc\s?([123])\b", t)
    if preset:
        return {"intent": "simulate", "scenario": {"id": "SC" + preset.group(1)}, "item": item, "preferences": prefs}
    delay = (re.search(r"delay\w*\D{0,25}?(?<![\d.])(\d+)\s*days?\b", t)
             or re.search(r"(?<![\d.])(\d+)\s*days?\s*(?:of\s+)?delay", t))
    surge = (re.search(r"demand\D{0,30}?(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*%", t)
             or re.search(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*%\s*(?:more|higher|increase|rise|surge|jump)?\s*(?:in\s+)?\w*\s*demand", t))
    spoil = re.search(r"heatwave|heat wave|fridge|cold[- ]chain (?:failure|break)", t)
    if delay or surge or spoil:
        sc = {}
        if delay:
            sc["delay_days"] = int(delay.group(1))
            sup = re.search(r"\bsup\s?0?([1-7])\b", t)
            named = _named_suppliers(t)
            if sup:
                sc["supplier_id"] = "SUP0" + sup.group(1)
            elif named:
                sc["supplier_id"] = named[0]
        if surge:
            m = 1 + float(surge.group(1)) / 100.0
            sc["demand_multiplier"] = {item: m} if item else m
        if spoil:
            sc["spoilage_pct"] = 0.25
        return {"intent": "simulate", "scenario": sc, "item": item, "preferences": prefs}
    if prefs:
        return {"intent": "set_preference", "preferences": prefs}
    if item:
        return {"intent": "simulate", "scenario": None, "item": item}
    if any(w in t for w in ("brief", "risk", "status", "today", "overview")):
        return {"intent": "brief"}
    return {"intent": "help"}


async def parse_intent(ctx: Context, text: str):
    if time.time() < MEM["llm_off_until"]:
        return keyword_parse(text), "keyword-fallback (LLM paused)"
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_asi1_sync, text), timeout=LLM_TIMEOUT)
        j = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        if j.get("intent") in ("simulate", "brief", "set_preference", "help", "none"):
            return j, "asi1"
    except Exception as e:
        err = str(e)
        if any(k in err for k in ("insufficient_quota", "403", "401", "No module named")):
            MEM["llm_off_until"] = time.time() + LLM_COOLDOWN
            ctx.logger.warning(f"LLM unavailable, pausing it for {LLM_COOLDOWN}s: {err[:120]}")
        else:
            ctx.logger.warning(f"ASI:One parse failed, using keyword fallback: {err[:120]}")
    return keyword_parse(text), "keyword-fallback"


# ---------------- orchestration ----------------
def _open(ctx: Context) -> list:
    return list(ctx.storage.get("open_rids") or [])


def close_req(ctx: Context, rid: str):
    ctx.storage.set("open_rids", [r for r in _open(ctx) if r != rid])
    ctx.storage.set(f"req_{rid}", None)


async def start_run(ctx, user, scenario, item, prefs, kind, intro=""):
    rid = uuid4().hex[:8]
    req = {"user": user, "scenario": scenario, "item": item, "prefs": prefs, "kind": kind,
           "intro": intro, "notes": [], "stage": "inventory", "t": time.time(), "source": ""}
    ctx.storage.set("open_rids", _open(ctx) + [rid])
    cache = MEM["inv_cache"]
    if cache and cache.get("items") and time.time() - cache["t"] < CACHE_MAX_AGE:
        req["source"] = f"Stock synced from Inventory Agent {int(time.time() - cache['t'])}s ago."
        ctx.logger.info(f"[{rid}] using synced inventory | kind={kind} scenario={scenario} item={item}")
        await send_sim(ctx, rid, req, cache["items"])
        return
    ctx.storage.set(f"req_{rid}", req)
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
    rows = sorted(items, key=lambda r: r["days_of_cover"]) if req["kind"] == "brief" else items
    for r in rows[:3]:
        L.append(f"- {r['item']}: {r['days_of_cover']} days of cover, stockout risk "
                 f"{pct(r['stockout_prob'])} ({r['severity']})")
    pending = None
    if f:
        L += ["", f"**{f['item']}**: stock {f['stock_now']:,}, demand {f['daily_demand']}/day, "
                  f"{f['days_of_cover']} days of cover, primary supplier "
                  f"{SUPPLIERS.get(f['primary_supplier'], f['primary_supplier'])} ({f['primary_lead_days']}d lead), "
                  f"stockout risk {pct(f['stockout_prob'])}, risk score {f['risk_score']}, "
                  f"expected unmet {f['expected_unmet_units']} units"]
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
                   "risk_now": f.get("stockout_prob"),
                   "scenario": sc.get("name", "the current disruption")}
    elif f and f.get("note"):
        L.append(f["note"])
    elif req["kind"] == "brief":
        L += ["", "No item needs action today. Ask a what-if, e.g. *What if SUP01 is delayed 10 days "
                  "and insulin demand rises 30%?*"]
    notes = req.get("notes", []) + meta.get("warnings", [])
    if "embedded" in str(meta.get("data_source", "")) and not req.get("notes"):
        notes.append("Used the Simulation Agent's stored snapshot (inventory data unavailable).")
    if req.get("source"):
        notes.append(req["source"])
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
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id="sync"))


@agent.on_interval(period=SYNC_PERIOD)
async def sync_inventory(ctx: Context):
    """Keep a live copy of the Inventory twin so user requests go straight to the Simulation Agent."""
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id="sync"))


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                               acknowledged_msg_id=msg.msg_id))
    ctx.logger.info(f"Chat from {sender[:16]}... content={[type(c).__name__ for c in msg.content]}")
    try:
        await handle_chat(ctx, sender, msg)
    except Exception as e:
        ctx.logger.error("Chat handler crashed: " + traceback.format_exc()[-1500:])
        await reply(ctx, sender, f"Sorry, MediTwin hit an internal error ({type(e).__name__}). Please try again.")


async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    text = " ".join(getattr(c, "text", "") or "" for c in msg.content
                    if getattr(c, "type", "") == "text" or isinstance(c, TextContent)).strip()
    if not text:
        ctx.logger.info("No text in message (session/metadata content); nothing to do")
        return
    mid = str(msg.msg_id)
    last = MEM["lasttext"].get(sender) or {}
    if mid in MEM["seen"] or (last.get("text") == text and time.time() - last.get("t", 0) < 20):
        ctx.logger.info("Duplicate message ignored")
        return
    MEM["seen"] = (MEM["seen"] + [mid])[-200:]
    MEM["lasttext"][sender] = {"text": text, "t": time.time()}
    ctx.logger.info(f"User {sender[:16]}...: {text[:300]}")
    t = text.lower()

    if re.search(r"\breset\b.*\b(preferences|prefs|settings)\b", t):
        for k in ("prefs", "pending", "last"):
            ctx.storage.set(f"{k}_{sender}", None)
        await reply(ctx, sender, "Preferences reset to defaults (no preferred supplier, budget cap Rs 600,000).")
        return

    # 1. pending approval (deterministic, no LLM needed)
    pending = ctx.storage.get(f"pending_{sender}")
    if pending:
        sel = parse_selection(text, pending)
        if sel == "cancel":
            t = "no"
        elif sel == "recommended":
            t = "yes"
        elif sel:
            t = f"option {sel}"
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
    if kind == "none":
        await reply(ctx, sender, "Noted. Ask for today's risk brief, a what-if scenario, or reply YES/NO to a pending plan.")
        return
    old = ctx.storage.get(f"prefs_{sender}") or {}
    new_prefs = {k: v for k, v in clean_prefs(intent.get("preferences")).items() if old.get(k) != v}
    if kind == "set_preference" and not new_prefs and clean_prefs(intent.get("preferences")):
        await reply(ctx, sender, "Those preferences are already saved; nothing changed.")
        return
    intro = ""
    if new_prefs:
        saved = {**old, **new_prefs}
        ctx.storage.set(f"prefs_{sender}", saved)
        bits = []
        if saved.get("preferred_suppliers"):
            bits.append("preferred suppliers " + ", ".join(SUPPLIERS[s] for s in saved["preferred_suppliers"]))
        if saved.get("budget_cap_inr"):
            bits.append("budget cap " + inr(saved["budget_cap_inr"]))
        intro = "Saved your preferences: " + "; ".join(bits) + ".\n"
        old = saved
    prefs = {**DEFAULT_PREFS, **old}

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
    if msg.items:
        MEM["inv_cache"] = {"items": msg.items, "t": time.time()}
    if msg.request_id == "sync":
        ctx.logger.info(f"Inventory synced: {len(msg.items)} items")
        return
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
    try:
        text, pending = format_result(req, items, meta)
    except Exception:
        ctx.logger.error("format_result crashed: " + traceback.format_exc()[-1500:])
        await reply(ctx, user, "The simulation finished but its result could not be formatted. Please try again.")
        return
    ctx.storage.set(f"pending_{user}", pending)
    if req["kind"] != "brief":
        ctx.storage.set(f"last_{user}", {"scenario": req.get("scenario"), "item": req.get("item")})
    ctx.logger.info(f"[{rid}] <- Simulation, replying to user")
    await reply(ctx, user, text, build_card(pending))


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