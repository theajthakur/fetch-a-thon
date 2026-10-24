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

try:
    from uagents_core.contrib.protocols.chat import EndSessionContent
except ImportError:
    EndSessionContent = None

try:
    from uagents_core.contrib.protocols.chat import MetadataContent
except ImportError:
    MetadataContent = None

INVENTORY_ADDR = os.getenv("INVENTORY_ADDR", "agent1qw5sdhru2eml9w08k65waf0rnqpxz6xazqvcmnen57t4p75gvgegjke70eg")
SIMULATION_ADDR = os.getenv("SIMULATION_ADDR", "agent1q229ftx9ae3vlwjy87hw7cnvaa6dm8g99q5lh0ky5e824kz69xcage63jj3")
INV_TIMEOUT = 15
SIM_TIMEOUT = 40
TRANSFER_COST_INR = 1500
LLM_TIMEOUT = 8
LLM_COOLDOWN = 600
SYNC_PERIOD = 60.0
CACHE_MAX_AGE = 300
IST = timezone(timedelta(hours=5, minutes=30))


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
    "SUP01": "Apex Pharma Distributors",
    "SUP02": "LifeLine Biologics",
    "SUP03": "Himalaya MedSupply",
    "SUP04": "GasPro Industrial Gases",
    "SUP05": "CityMed Wholesale",
    "SUP06": "SafeGuard Surgicals",
    "SUP07": "Bharat Generics",
}

ITEM_WORDS = [
    "insulin", "hepatitis", "vaccine", "oxytocin", "oxygen", "adrenaline",
    "epinephrine", "ceftriaxone", "saline", "heparin", "gloves", "n95",
    "mask", "cannula", "paracetamol",
]
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

HELP = (
    "I'm **MediTwin**, a digital twin of City General Hospital's medical supply chain. "
    "I simulate disruptions, predict stockouts and draft fix plans for approval.\n\nTry:\n"
    "- Give me today's risk brief\n"
    "- What if SUP01 is delayed 10 days and insulin demand rises 30%?\n"
    "- Run scenario SC3\n"
    "- Prefer LifeLine Biologics and keep my budget under 5 lakh"
)
FOOTER = "_Synthetic data. Logistics decision support, not clinical advice._"

MEM = {"seen": [], "lasttext": {}, "llm_off_until": 0.0, "inv_cache": None}

agent = Agent()
chat_proto = Protocol(spec=chat_protocol_spec)


def make_reply(text: str, card: dict = None) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if card and MetadataContent is not None:
        content.append(MetadataContent(type="metadata", metadata=card))
    elif EndSessionContent is not None:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


async def reply(ctx: Context, to: str, text: str, card: dict = None):
    ctx.logger.info(f"Sending reply to {to[:16]}... (card={'yes' if card else 'no'})")
    await ctx.send(to, make_reply(text, card))


CARDS_ENABLED = True
CARD_KIND = "detail"


def card_selection(i, o):
    return {"action": "approve_plan", "option": str(i), "plan_id": o["id"]}


def build_card_payload(pending):
    opts, rec = pending.get("options") or [], pending.get("recommended")
    if not opts:
        return None
    ordered = sorted(opts, key=lambda o: o["id"] != rec)
    rec_opt = next((o for o in opts if o["id"] == rec), None)
    rows = [
        {"label": "Item", "value": pending["item"]},
        {"label": "Scenario", "value": pending.get("scenario", "-")},
    ]
    if pending.get("risk_now") is not None:
        rows.append({"label": "Stockout risk if no action", "value": pct(pending["risk_now"])})
    if rec_opt:
        rows.append({"label": "Recommended", "value": f"{rec_opt['label']} ({inr(rec_opt['total_cost_inr'])})"})

    choices = [
        {
            "value": o["id"],
            "label": ("Recommended: " if o["id"] == rec else "") + o["label"],
            "secondary_text": (
                f"Stockout {risk_label(o)} | {inr(o['total_cost_inr'])} | {o['lead_days']}d"
                + ("" if o.get("within_budget", True) else " | over budget")
            ),
        }
        for o in ordered
    ]
    return {
        "title": f"Fix plan for {pending['item']}",
        "summary_rows": rows,
        "sub_options": {"name": "plan_id", "kind": "radio", "label": "Choose a plan", "choices": choices},
        "ctas": [
            {"label": "Approve & draft PO", "selection": {"action": "approve_plan"}, "primary": True},
            {"label": "Cancel", "selection": {"action": "cancel_plan"}},
        ],
    }


def build_card(pending):
    if not (CARDS_ENABLED and pending):
        return None
    try:
        payload = build_card_payload(pending)
        if not payload:
            return None
        return {
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": CARD_KIND,
            "card_payload": json.dumps(payload),
            "preferred_drawer_width_px": "540",
        }
    except Exception:
        return None


def parse_selection(text, pending):
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
                return "recommended"
    except (ValueError, TypeError):
        pass

    low = text.lower()
    if "cancel_plan" in low:
        return "cancel"
    for i, o in enumerate(opts, 1):
        if o["id"].lower() in low:
            return i
    if "approve_plan" in low:
        return "recommended"
    return None


def risk_label(r) -> str:
    """0 simulated stockouts is reported with its 95% upper bound, never as "0%" (engine 1.1 fields)."""
    k, n = r.get("stockout_runs"), r.get("runs")
    p = r.get("stockout_prob") or 0
    if k == 0 and n:
        return f"0/{n:,} runs (95% bound <{(r.get('stockout_prob_upper95') or 3.0 / n) * 100:.1f}%)"
    return f"{p * 100:.1f}%" if p < 0.1 else f"{round(p * 100)}%"


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
    out = {
        k: sc[k]
        for k in ("supplier_id", "delay_days", "demand_multiplier", "spoilage_pct")
        if sc.get(k) not in (None, "", {}, 0)
    }
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


def _asi1_sync(text: str) -> str:
    from openai import OpenAI
    client = OpenAI(
        base_url=os.getenv("ASI1_BASE_URL", "https://api.asi1.ai/v1"),
        api_key=os.getenv("ASI1_API_KEY", ""),
    )
    r = client.chat.completions.create(
        model="asi1",
        temperature=0,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
    )
    return r.choices[0].message.content or ""


def _budget(t):
    for pat in (
        r"budget\D{0,25}?(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|k)?\b",
        r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|k)\b[^.]{0,20}budget",
    ):
        m = re.search(pat, t)
        if m:
            v = float(m.group(1).replace(",", "")) * {"lakh": 1e5, "lac": 1e5, "k": 1e3}.get(m.group(2) or "", 1)
            if v >= 10000:
                return int(v)
    return None


def _named_suppliers(t):
    return [
        sid for sid, n in SUPPLIERS.items()
        if re.search(r"\b" + sid.lower() + r"\b", t) or n.lower().split()[0] in t
    ]


def keyword_parse(text: str) -> dict:
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

    delay = (
        re.search(r"delay\w*\D{0,25}?(?<![\d.])(\d+)\s*days?\b", t)
        or re.search(r"(?<![\d.])(\d+)\s*days?\s*(?:of\s+)?delay", t)
    )
    surge = (
        re.search(r"demand\D{0,30}?(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*%", t)
        or re.search(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*%\s*(?:more|higher|increase|rise|surge|jump)?\s*(?:in\s+)?\w*\s*demand", t)
    )
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
        return keyword_parse(text), "fallback (paused)"
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_asi1_sync, text), timeout=LLM_TIMEOUT)
        j = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        if j.get("intent") in ("simulate", "brief", "set_preference", "help", "none"):
            return j, "asi1"
    except Exception as e:
        err = str(e)
        if any(k in err for k in ("insufficient_quota", "403", "401", "No module named")):
            MEM["llm_off_until"] = time.time() + LLM_COOLDOWN
            ctx.logger.warning(f"LLM paused ({LLM_COOLDOWN}s): {err[:100]}")
        else:
            ctx.logger.warning(f"ASI:One parse error, using fallback: {err[:100]}")
    return keyword_parse(text), "fallback"


def _open(ctx: Context) -> list:
    return list(ctx.storage.get("open_rids") or [])


def close_req(ctx: Context, rid: str):
    ctx.storage.set("open_rids", [r for r in _open(ctx) if r != rid])
    ctx.storage.set(f"req_{rid}", None)


async def start_run(ctx, user, scenario, item, prefs, kind, intro=""):
    rid = uuid4().hex[:8]
    req = {
        "user": user, "scenario": scenario, "item": item, "prefs": prefs, "kind": kind,
        "intro": intro, "notes": [], "stage": "inventory", "t": time.time(), "source": "",
    }
    ctx.storage.set("open_rids", _open(ctx) + [rid])
    cache = MEM["inv_cache"]
    if cache and cache.get("items") and time.time() - cache["t"] < CACHE_MAX_AGE:
        req["source"] = f"Stock synced from inventory {int(time.time() - cache['t'])}s ago."
        await send_sim(ctx, rid, req, cache["items"])
        return
    ctx.storage.set(f"req_{rid}", req)
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id=rid, focus_items=[]))


async def send_sim(ctx, rid, req, items):
    sc = dict(req.get("scenario") or {})
    if req.get("item"):
        sc["item"] = req["item"]
    sc["preferences"] = req.get("prefs") or {}
    req.update(stage="simulation", t=time.time())
    ctx.storage.set(f"req_{rid}", req)
    await ctx.send(SIMULATION_ADDR, SimRequest(request_id=rid, items=items, scenario=sc))


def format_result(req, items, meta):
    s, sc, f = meta.get("summary", {}), meta.get("scenario", {}), meta.get("focus")
    head = {
        "brief": "Today's risk brief",
        "rerun": "Re-run with your updated preferences",
    }.get(req["kind"], "Scenario: " + sc.get("name", "custom"))

    lines = [req["intro"]] if req.get("intro") else []
    lines += [
        f"**{head}**",
        f"Risk overview: {s.get('critical', 0)} critical, {s.get('high', 0)} high, "
        f"{s.get('medium', 0)} medium, {s.get('low', 0)} low",
        "",
    ]
    rows = sorted(items, key=lambda r: r["days_of_cover"]) if req["kind"] == "brief" else items
    for r in rows[:3]:
        lines.append(
            f"- {r['item']}: {r['days_of_cover']} days of cover, simulated stockout "
            f"{risk_label(r)} ({r['severity']})"
        )

    pending = None
    if f:
        sup_label = SUPPLIERS.get(f['primary_supplier'], f['primary_supplier'])
        lines += [
            "",
            f"**{f['item']}**: stock {f['stock_now']:,}, demand {f['daily_demand']}/day, "
            f"{f['days_of_cover']} days of cover, primary supplier {sup_label} ({f['primary_lead_days']}d lead), "
            f"simulated stockout {risk_label(f)}, risk score {f['risk_score']}, "
            f"expected unmet {f['expected_unmet_units']} units",
        ]

    if f and f.get("options"):
        rec, allopts = f.get("recommended"), f["options"]
        opts = allopts[:4]
        if rec and all(o["id"] != rec for o in opts):
            opts = [o for o in allopts if o["id"] == rec] + allopts[:3]

        lines += [
            "",
            f"**Fix options for {f['item']}** (current plan: simulated stockout {risk_label(f)}, "
            f"first stockout ~day {f.get('expected_first_stockout_day') or '-'})",
        ]
        for i, o in enumerate(opts, 1):
            budget_flag = "" if o["within_budget"] else " (over budget)"
            rec_flag = "  **<- recommended**" if o["id"] == rec else ""
            lines.append(f"{i}. {o['label']} | stockout {risk_label(o)} | {inr(o['total_cost_inr'])}{budget_flag}{rec_flag}")

        if f.get("recommendation_reason"):
            lines.append("Why: " + f["recommendation_reason"])
        lines += ["", "Reply **YES** to approve the recommended plan, **option N** to pick another, or **NO** to cancel."]

        pending = {
            "item": f["item"], "item_id": f.get("item_id"), "options": opts, "recommended": rec,
            "risk_now": f.get("stockout_prob"), "scenario": sc.get("name", "the current disruption"),
        }
    elif f and f.get("note"):
        lines.append(f["note"])
    elif req["kind"] == "brief":
        lines += ["", "No item needs action today. Ask a what-if to test a disruption scenario."]

    notes = req.get("notes", []) + meta.get("warnings", [])
    if "embedded" in str(meta.get("data_source", "")) and not req.get("notes"):
        notes.append("Used stored snapshot as inventory data was unavailable.")
    if req.get("source"):
        notes.append(req["source"])

    lines += [""] + [f"_Note: {n}_" for n in notes] + [FOOTER]
    return "\n".join(lines), pending


PAYMENT_HANDLE = os.getenv("PAYMENT_HANDLE", "@meditwin-payment")
# Optional in-chat payment step. Leave PAYMENT_ADDR unset to keep the original flow exactly as it was.
PAYMENT_ADDR = os.getenv("PAYMENT_ADDR", "").strip()
PAYMENT_TTL = 30 * 60
ADVANCE_PCT = 10


def payment_offer(o, po_no):
    """Supplier name and supplier-only cost of an approved plan (transfers are not paid to the supplier)."""
    sid = o.get("supplier_id")
    qty = o.get("supplier_order_qty", o.get("order_qty", 0))
    if not (sid and qty):
        return None
    cost = o.get("supplier_cost_inr", o["total_cost_inr"] - sum(t.get("cost_inr", TRANSFER_COST_INR)
                                                                for t in (o.get("transfers") or [])))
    return {"po": po_no, "supplier": SUPPLIERS.get(sid, sid), "total": cost, "t": time.time()}


def payment_offer_card(offer) -> dict:
    payload = {"title": "Pay the supplier advance?",
               "summary_rows": [{"label": "PO", "value": offer["po"]},
                                {"label": "Supplier", "value": offer["supplier"]},
                                {"label": f"Advance ({ADVANCE_PCT}%)", "value": inr(offer["total"] * ADVANCE_PCT / 100)},
                                {"label": "PO total", "value": inr(offer["total"])}],
               "approve_cta": {"label": f"Pay {ADVANCE_PCT}% advance",
                               "selection": {"action": "pay_advance", "po": offer["po"]}, "primary": True},
               "reject_cta": {"label": "Skip for now", "selection": {"action": "skip_payment", "po": offer["po"]}}}
    return {"card_protocol_version": "1", "requires_card_interaction": "true", "card_kind": "review",
            "card_payload": json.dumps(payload), "preferred_drawer_width_px": "480"}


def payment_action(text: str):
    """Card clicks and typed commands for the payment step -> (action, raw_text_to_forward)."""
    try:
        j = json.loads(text)
        if isinstance(j, dict) and j.get("action") in ("pay_advance", "skip_payment", "verify_payment", "cancel_payment"):
            return j["action"], text
    except (ValueError, TypeError):
        pass
    low = text.lower()
    for act in ("pay_advance", "skip_payment", "verify_payment", "cancel_payment"):
        if act in low:
            return act, text
    if re.search(r"\bpay\b.*\badvance\b|^\s*pay( now)?\s*$", low):
        return "pay_advance", text
    if re.search(r"^\s*(verify|i'?ve paid|i paid|paid|done)\b", low):
        return "verify_payment", "verify"
    if re.search(r"\bcancel payment\b", low):
        return "cancel_payment", "cancel payment"
    return None, None


async def relay_from_payment(ctx: Context, msg: ChatMessage):
    """Forward the Payment Agent's reply (text, Stripe link, card) to the user who asked to pay."""
    relay = ctx.storage.get("pay_relay") or {}
    if not relay.get("user") or time.time() - relay.get("t", 0) > PAYMENT_TTL:
        ctx.logger.info("Payment Agent reply with no active payment relay; ignored")
        return
    content, first, has_card = [], True, False
    for c in msg.content:
        if isinstance(c, TextContent) or getattr(c, "type", "") == "text":
            txt = getattr(c, "text", "") or ""
            content.append(TextContent(type="text", text=("**Payment Agent:** " + txt) if first else txt))
            first = False
        elif MetadataContent is not None and getattr(c, "type", "") == "metadata":
            content.append(MetadataContent(type="metadata", metadata=c.metadata))
            has_card = True
    if not content:
        return
    if not has_card and EndSessionContent is not None:
        content.append(EndSessionContent(type="end-session"))
    ctx.logger.info(f"Relaying Payment Agent reply to {relay['user'][:12]} (card={'yes' if has_card else 'no'})")
    await ctx.send(relay["user"], ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content))


async def handle_payment(ctx: Context, sender: str, text: str) -> bool:
    """In-chat payment step through the separate Payment Agent. Returns True when the message was handled."""
    if not PAYMENT_ADDR:
        return False
    action, raw = payment_action(text)
    relay = ctx.storage.get("pay_relay") or {}
    relay_active = relay.get("user") and time.time() - relay.get("t", 0) < PAYMENT_TTL
    if action in ("verify_payment", "cancel_payment") and not relay_active and raw in ("verify", "cancel payment"):
        return False  # a plain word with no payment in progress: let the normal flow handle it
    if not action:
        return False
    if action == "skip_payment":
        await reply(ctx, sender, f"OK, no payment made. Say `pay advance` any time in the next 30 minutes to pay it.")
        return True
    if action == "pay_advance":
        offer = ctx.storage.get("pay_offer") or {}
        if not offer or time.time() - offer.get("t", 0) > PAYMENT_TTL:
            await reply(ctx, sender, "There is no recently approved PO to pay. Approve a plan first.")
            return True
        raw = (f"pay {ADVANCE_PCT}% advance for {offer['po']}, {offer['supplier']}, total {inr(offer['total'])}")
    ctx.storage.set("pay_relay", {"user": sender, "t": time.time()})
    ctx.logger.info(f"Forwarding to Payment Agent: {raw[:120]}")
    await ctx.send(PAYMENT_ADDR, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                             content=[TextContent(type="text", text=raw)]))
    return True


def make_po(p, o):
    now = datetime.now(IST)
    po_no = f"PO-{now:%Y%m%d}-{uuid4().hex[:4].upper()}"
    transfers = o.get("transfers") or []
    sid = o.get("supplier_id")
    # Engine 1.1 sends supplier and transfer quantities/costs separately; never derive them from the total.
    order_qty = o.get("supplier_order_qty", o.get("order_qty", 0))
    lines = [f"**Approved: {o['label']}**", ""]
    pay_hint = None

    if sid and order_qty:
        sup = SUPPLIERS.get(sid, sid)
        supplier_cost = o.get("supplier_cost_inr",
                              o["total_cost_inr"] - sum(t.get("cost_inr", TRANSFER_COST_INR) for t in transfers))
        unit = o.get("supplier_unit_cost_inr") or supplier_cost / order_qty
        exp = o.get("type") == "expedited_order"
        # Hand-off to the separate Payment Agent: text only, so this flow never depends on payments working.
        pay_hint = f"{PAYMENT_HANDLE} pay 10% advance for {po_no}, {sup}, total {inr(supplier_cost)}"
        lines += [
            "**Purchase order**", "```",
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
            "Regards,", "Procurement Team, City General Hospital", "",
        ]

    for t in transfers:
        lines.append(
            f"**Transfer request to {t['name']}**: please send {t['qty']:,} units of {p['item']} today "
            f"(ETA ~{t.get('eta_days', 1)}d, est. logistics {inr(t.get('cost_inr', TRANSFER_COST_INR))})."
        )
    if not transfers and o.get("type") in ("transfer", "bridge"):
        lines.append("**Transfer request** sent to sister hospitals for their spare units.")

    lines += [
        "",
        f"Total plan cost: {inr(o['total_cost_inr'])}. Simulated stockout after this plan: {risk_label(o)} (model estimate, not a guarantee).",
    ]
    if pay_hint and PAYMENT_ADDR:
        lines += ["", "**Next:** pay the supplier advance with the card below (Stripe test mode)."]
    elif pay_hint:
        lines += ["", f"**Next: pay the supplier advance** (Stripe test mode) by sending:", f"`{pay_hint}`"]
    lines += [FOOTER]
    return po_no, "\n".join(lines)


@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Action Agent online: {ctx.agent.address}")
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id="sync"))


@agent.on_interval(period=SYNC_PERIOD)
async def sync_inventory(ctx: Context):
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id="sync"))


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    try:
        await handle_chat(ctx, sender, msg)
    except Exception as e:
        ctx.logger.error(f"Chat processing failed: {traceback.format_exc()[-1000:]}")
        await reply(ctx, sender, f"MediTwin encountered an internal error ({type(e).__name__}). Please retry.")


PENDING_TTL = 900  # seconds an approval stays valid for a different ASI:One session address


def save_pending(ctx: Context, user: str, pending):
    """Pending approvals are stored per sender and as 'latest', because ASI:One may deliver a card click
    or a follow-up YES from a different session address than the one that received the options."""
    ctx.storage.set(f"pending_{user}", pending)
    ctx.storage.set("pending_latest", {"user": user, "pending": pending, "t": time.time()} if pending else None)


def clear_pending(ctx: Context, *users):
    for u in users:
        if u:
            ctx.storage.set(f"pending_{u}", None)
    ctx.storage.set("pending_latest", None)


def strip_mentions(text: str) -> str:
    """'@supply-chain-action YES' -> 'YES' (ASI:One keeps the @handle in the text)."""
    return re.sub(r"^\s*(?:@\[?[\w.\-]+\]?[:,]?\s*)+", "", text).strip()


async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    text = " ".join(
        getattr(c, "text", "") or "" for c in msg.content
        if getattr(c, "type", "") == "text" or isinstance(c, TextContent)
    ).strip()
    if not text:
        return
    if PAYMENT_ADDR and sender == PAYMENT_ADDR:
        await relay_from_payment(ctx, msg)
        return

    mid = str(msg.msg_id)
    last = MEM["lasttext"].get(sender) or {}
    is_payment_step = bool(PAYMENT_ADDR and payment_action(strip_mentions(text) or text)[0])
    if mid in MEM["seen"] or (not is_payment_step and last.get("text") == text and time.time() - last.get("t", 0) < 20):
        return

    MEM["seen"] = (MEM["seen"] + [mid])[-200:]
    MEM["lasttext"][sender] = {"text": text, "t": time.time()}
    ctx.logger.info(f"Incoming message from {sender[:12]}: {text[:150]}")
    text = strip_mentions(text) or text
    t = text.lower()
    if await handle_payment(ctx, sender, text):
        return

    if re.search(r"\breset\b.*\b(preferences|prefs|settings)\b", t):
        for k in ("prefs", "pending", "last"):
            ctx.storage.set(f"{k}_{sender}", None)
        await reply(ctx, sender, "Preferences reset to defaults (no preferred supplier, budget cap Rs 600,000).")
        return

    pending, owner = ctx.storage.get(f"pending_{sender}"), sender
    if not pending:
        latest = ctx.storage.get("pending_latest") or {}
        if latest.get("pending") and time.time() - latest.get("t", 0) < PENDING_TTL:
            cand = latest["pending"]
            looks_like_decision = (parse_selection(text, cand) is not None or re.match(
                r"\s*(yes|y|approve|approved|confirm|go ahead|ok|okay|no|cancel|reject|stop|option\s*#?\s*\d+)\b", t))
            if looks_like_decision:
                pending, owner = cand, latest.get("user")
                ctx.logger.info(f"Using latest pending plan from session {str(owner)[:12]}")
    if not pending and parse_selection(text, {"options": []}) is not None:
        await reply(ctx, sender, "That plan is no longer pending. Run the what-if again to get fresh options.")
        return
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
                idx = int(opt.group(1)) - 1
                if not 0 <= idx < len(opts):
                    await reply(ctx, sender, f"Please select option 1 to {len(opts)}.")
                    return
                choice = opts[idx]
            else:
                choice = next((x for x in opts if x["id"] == pending.get("recommended")), opts[0])

            po_no, doc = make_po(pending, choice)
            hist = (ctx.storage.get(f"orders_{sender}") or [])[-9:]
            ctx.storage.set(
                f"orders_{sender}",
                hist + [{"po": po_no, "item": pending["item"], "plan": choice["label"], "cost": choice["total_cost_inr"]}],
            )
            clear_pending(ctx, sender, owner)
            offer = payment_offer(choice, po_no) if PAYMENT_ADDR else None
            if offer:
                ctx.storage.set("pay_offer", offer)
            await reply(ctx, sender, doc, payment_offer_card(offer) if offer else None)
            return

        if re.match(r"\s*(no|cancel|reject|stop)\b", t):
            clear_pending(ctx, sender, owner)
            await reply(ctx, sender, "Cancelled. No order was placed.")
            return

    intent, src = await parse_intent(ctx, text)
    kind = intent.get("intent")
    if kind == "none":
        await reply(ctx, sender, "Noted. Ask for today's risk brief, run a what-if, or confirm pending plans.")
        return

    old_prefs = ctx.storage.get(f"prefs_{sender}") or {}
    new_prefs = {k: v for k, v in clean_prefs(intent.get("preferences")).items() if old_prefs.get(k) != v}
    if kind == "set_preference" and not new_prefs and clean_prefs(intent.get("preferences")):
        await reply(ctx, sender, "Those preferences are already active.")
        return

    intro = ""
    if new_prefs:
        saved = {**old_prefs, **new_prefs}
        ctx.storage.set(f"prefs_{sender}", saved)
        bits = []
        if saved.get("preferred_suppliers"):
            bits.append("preferred suppliers: " + ", ".join(SUPPLIERS[s] for s in saved["preferred_suppliers"]))
        if saved.get("budget_cap_inr"):
            bits.append("budget cap: " + inr(saved["budget_cap_inr"]))
        intro = "Saved preferences: " + "; ".join(bits) + ".\n"
        old_prefs = saved

    active_prefs = {**DEFAULT_PREFS, **old_prefs}

    if kind == "set_preference":
        last_run = ctx.storage.get(f"last_{sender}")
        if not new_prefs:
            await reply(ctx, sender, "Could not find a supplier or budget specification. " + HELP)
        elif last_run:
            await start_run(ctx, sender, last_run.get("scenario"), last_run.get("item"), active_prefs, "rerun", intro)
        else:
            await reply(ctx, sender, intro + "Applied to upcoming simulations.")
        return

    if kind == "brief":
        await start_run(ctx, sender, None, None, active_prefs, "brief", intro)
        return

    if kind == "simulate":
        await start_run(ctx, sender, clean_scenario(intent.get("scenario")), intent.get("item"), active_prefs, "simulate", intro)
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
        ctx.logger.info(f"Inventory refreshed: {len(msg.items)} items")
        return

    req = ctx.storage.get(f"req_{msg.request_id}")
    if not req or req.get("stage") != "inventory":
        return

    await send_sim(ctx, msg.request_id, req, msg.items)


@agent.on_message(model=SimResult)
async def on_result(ctx: Context, sender: str, msg: SimResult):
    rid = msg.request_id
    req = ctx.storage.get(f"req_{rid}")
    if not req or req.get("stage") != "simulation":
        return

    user = req["user"]
    close_req(ctx, rid)

    if not msg.results or "error" in msg.results[0]:
        err_msg = msg.results[0].get("error") if msg.results else "empty result"
        await reply(ctx, user, f"The simulation could not run ({err_msg}). Please retry.")
        return

    meta = next((r["meta"] for r in msg.results if "meta" in r), {})
    items = [r for r in msg.results if "item_id" in r]
    try:
        text, pending = format_result(req, items, meta)
    except Exception:
        ctx.logger.error(f"Formatting failed: {traceback.format_exc()[-1000:]}")
        await reply(ctx, user, "Simulation completed but result formatting failed.")
        return

    save_pending(ctx, user, pending)
    if req["kind"] != "brief":
        ctx.storage.set(f"last_{user}", {"scenario": req.get("scenario"), "item": req.get("item")})

    await reply(ctx, user, text, build_card(pending))


@agent.on_interval(period=5.0)
async def watchdog(ctx: Context):
    for rid in _open(ctx):
        req = ctx.storage.get(f"req_{rid}")
        if not req:
            close_req(ctx, rid)
            continue

        elapsed = time.time() - req.get("t", 0)
        if req["stage"] == "inventory" and elapsed > INV_TIMEOUT:
            ctx.logger.warning(f"[{rid}] Inventory timeout, falling back to embedded data")
            req["notes"] = req.get("notes", []) + ["Inventory Agent did not respond; using embedded baseline snapshot."]
            await send_sim(ctx, rid, req, [])
        elif req["stage"] == "simulation" and elapsed > SIM_TIMEOUT:
            ctx.logger.warning(f"[{rid}] Simulation timeout")
            close_req(ctx, rid)
            await reply(ctx, req["user"], "Simulation Agent is currently unreachable. Please retry shortly.")


agent.include(chat_proto, publish_manifest=True)