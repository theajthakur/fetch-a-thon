"""MediTwin Payment Agent (hosted on Agentverse as "meditwin-payment", handle @meditwin-payment).

Independent of the Action Agent: it never changes the purchase-order flow. After a PO has been approved, the user
asks this agent to pay a supplier advance, for example:
    @meditwin-payment pay 10% advance for PO-20261024-373E, Himalaya MedSupply, total Rs 361,920

Default settlement: FET (fet_direct) on the Fetch.ai testnet, verified on-chain with cosmpy, as in Fetch.ai's
official fet_direct example. Card payments (Stripe test mode) remain available with "by card".
Optional env: DEFAULT_PAY_METHOD ("fet" | "stripe"), FET_AMOUNT (default "0.1"), FET_USE_TESTNET (default "true").

Stripe flow (Agent Payment Protocol, seller role):
  1. Agent creates a Stripe Checkout Session (TEST MODE ONLY, embedded UI) and sends RequestPayment.
  2. ASI:One renders the Stripe payment card; the user pays with a test card and approves -> CommitPayment.
  3. Agent verifies the session with Stripe (paid, right amount, right PO) -> CompletePayment + receipt,
     otherwise CancelPayment with the reason. No real money moves: live Stripe keys are refused.

Secrets (Agentverse agent secrets): STRIPE_SECRET_KEY (sk_test_...), STRIPE_PUBLISHABLE_KEY (pk_test_...).
Optional: STRIPE_CURRENCY (default "inr"), INR_PER_USD (default "84", used only when STRIPE_CURRENCY="usd"),
STRIPE_SUCCESS_URL, DEFAULT_ADVANCE_PCT (default "10").
Synthetic data; logistics decision support, not clinical advice."""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)
try:  # interactive review card (Agent Chat Protocol metadata)
    from uagents_core.contrib.protocols.chat import MetadataContent
except ImportError:
    MetadataContent = None
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

STRIPE_API = "https://api.stripe.com/v1"
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()
STRIPE_CURRENCY = (os.getenv("STRIPE_CURRENCY", "usd") or "usd").strip().lower()
INR_PER_USD = float(os.getenv("INR_PER_USD", "84") or 84)
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "https://agentverse.ai/payment-success").strip()
DEFAULT_ADVANCE_PCT = float(os.getenv("DEFAULT_ADVANCE_PCT", "10") or 10)
DEFAULT_PAY_METHOD = (os.getenv("DEFAULT_PAY_METHOD", "fet") or "fet").strip().lower()   # "fet" or "stripe"
FET_AMOUNT = os.getenv("FET_AMOUNT", "0.1").strip()        # demo settlement amount in testnet FET
FET_USE_TESTNET = os.getenv("FET_USE_TESTNET", "true").lower() == "true"
FET_DENOM = "atestfet" if FET_USE_TESTNET else "afet"
MAX_PAYMENT_INR = 500000          # hard cap per payment (test mode, but keep amounts sensible)
PENDING_TTL = 30 * 60             # Stripe sessions expire after 30 min
DEADLINE_SECONDS = 600

SUPPLIERS = {
    "SUP01": "Apex Pharma Distributors", "SUP02": "LifeLine Biologics", "SUP03": "Himalaya MedSupply",
    "SUP04": "GasPro Industrial Gases", "SUP05": "CityMed Wholesale", "SUP06": "SafeGuard Surgicals",
    "SUP07": "Bharat Generics",
}

HELP = ("I'm the **MediTwin Payment Agent**. After MediTwin approves a purchase order, I collect the supplier "
        "advance through the Agent Payment Protocol: in **FET** on the Fetch.ai testnet (default) or by card "
        "(Stripe test mode). I verify every payment before issuing a receipt.\n\n"
        "Try:\n"
        "- `pay 10% advance for PO-20261024-373E, Himalaya MedSupply, total Rs 361,920`\n"
        "- `pay ... by card` to use Stripe instead\n"
        "- `verify tx <hash>`, `payment status`, `cancel payment`")

agent = Agent()
chat_proto = Protocol(spec=chat_protocol_spec)
payment_proto = Protocol(spec=payment_protocol_spec, role="seller")


# ---------------- helpers ----------------
def make_chat(text: str, card: dict = None) -> ChatMessage:
    content = [TextContent(type="text", text=text)]
    if card and MetadataContent is not None:
        content.append(MetadataContent(type="metadata", metadata=card))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


async def say(ctx: Context, to: str, text: str, card: dict = None):
    await ctx.send(to, make_chat(text, card))


def payment_card(p: dict) -> dict:
    """ASI:One 'review' card: pay via the Stripe link, then verify or cancel."""
    rows = [{"label": "PO", "value": p["po"]},
            {"label": "Supplier", "value": p["supplier"] or "supplier"},
            {"label": "Advance", "value": f"{inr(p['amount_inr'])} ({p['pct']:g}% of {inr(p['po_total_inr'])})"}]
    if p.get("method") == "fet":
        rows += [{"label": "Settle with", "value": f"{p['fet_amount']} FET ({'Fetch.ai testnet' if FET_USE_TESTNET else 'mainnet'})"},
                 {"label": "Pay to wallet", "value": p["wallet"]}]
    else:
        rows.append({"label": "Charged", "value": f"{p['currency']} {p['display_amount']} (Stripe test mode)"})
    payload = {"title": "Supplier advance payment",
               "summary_rows": rows,
               "approve_cta": {"label": "I've paid, verify",
                               "selection": {"action": "verify_payment", "reference": p["reference"]}, "primary": True},
               "reject_cta": {"label": "Cancel", "selection": {"action": "cancel_payment", "reference": p["reference"]}}}
    return {"card_protocol_version": "1", "requires_card_interaction": "true", "card_kind": "review",
            "card_payload": json.dumps(payload), "preferred_drawer_width_px": "480"}


def inr(x) -> str:
    return f"Rs {int(round(x or 0)):,}"


def stripe_ready():
    """Returns an error string, or None when test-mode keys are configured."""
    if not (STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY):
        return "Payments are not configured yet (missing STRIPE_SECRET_KEY / STRIPE_PUBLISHABLE_KEY secrets)."
    if not (STRIPE_SECRET_KEY.startswith("sk_test_") and STRIPE_PUBLISHABLE_KEY.startswith("pk_test_")):
        return "Refusing to run: only Stripe TEST keys (sk_test_/pk_test_) are allowed for this demo."
    return None


def strip_mentions(text: str) -> str:
    return re.sub(r"^\s*(?:@\[?[\w.\-]+\]?[:,]?\s*)+", "", text).strip()


def parse_payment_request(text: str) -> dict:
    """'pay 10% advance for PO-20261024-373E, Himalaya MedSupply, total Rs 361,920' -> fields."""
    t = text.lower()
    po = re.search(r"\bpo-\d{8}-[a-z0-9]{4}\b", t)
    amounts = [float(a.replace(",", "")) for a in
               re.findall(r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)", t) if a.replace(",", "").replace(".", "").isdigit()]
    pct = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%", t)
    supplier = next((n for sid, n in SUPPLIERS.items() if sid.lower() in t or n.lower() in t), None)
    if pct:
        p = float(pct.group(1))
    elif re.search(r"\b(full|entire|whole)\b", t):
        p = 100.0
    else:
        p = DEFAULT_ADVANCE_PCT
    return {"po": po.group(0).upper() if po else None,
            "po_total_inr": max(amounts) if amounts else None,   # the PO total, not a Rs 1,500 transfer line
            "pct": max(1.0, min(100.0, p)), "supplier": supplier}


def to_minor_units(amount_inr: float):
    """INR amount -> (Stripe minor units, currency code shown in Funds, display amount string)."""
    if STRIPE_CURRENCY == "usd":
        usd = amount_inr / INR_PER_USD
        return int(round(usd * 100)), "USD", f"{usd:.2f}"
    return int(round(amount_inr * 100)), STRIPE_CURRENCY.upper(), f"{amount_inr:.2f}"


def stripe_create_session(*, amount_minor: int, po: str, supplier: str, pct: float, reference: str, user: str) -> dict:
    """Embedded Checkout Session via Stripe's REST API (no SDK needed on hosted agents)."""
    data = {
        "mode": "payment",
        "success_url": f"{STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": STRIPE_SUCCESS_URL,
        "expires_at": str(int(time.time()) + PENDING_TTL),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": STRIPE_CURRENCY,
        "line_items[0][price_data][unit_amount]": str(amount_minor),
        "line_items[0][price_data][product_data][name]": f"Supplier advance {po}",
        "line_items[0][price_data][product_data][description]":
            f"{pct:g}% advance to {supplier or 'supplier'} for {po} (MediTwin demo, Stripe test mode)",
        "metadata[po]": po,
        "metadata[reference]": reference,
        "metadata[user]": user[:200],
        "metadata[service]": "meditwin_supplier_advance",
    }
    # Stripe API versions differ: newer accounts want ui_mode="embedded_page" and may reject older parameters.
    # Adapt to the account's version instead of failing.
    for _ in range(4):
        r = requests.post(f"{STRIPE_API}/checkout/sessions", data=data, auth=(STRIPE_SECRET_KEY, ""), timeout=20)
        body = r.json()
        if r.status_code < 400:
            break
        err = body.get("error", {}).get("message", f"Stripe error {r.status_code}")
        param = str(body.get("error", {}).get("param") or "")
        if "ui_mode" in err or param == "ui_mode":
            new = "embedded" if data["ui_mode"] == "embedded_page" else "embedded_page"
            if new in err or data["ui_mode"] == "embedded_page":
                data["ui_mode"] = new
                continue
        if "redirect_on_completion" in err or param == "redirect_on_completion":
            data.pop("redirect_on_completion", None)
            continue
        if "return_url" in err or param == "return_url":
            data.pop("return_url", None)
            continue
        raise RuntimeError(err)
    else:
        raise RuntimeError(err)
    # RequestPayment.metadata only accepts string values.
    out = {"id": body["id"], "checkout_session_id": body["id"], "url": body.get("url") or "",
           "client_secret": body.get("client_secret") or "", "publishable_key": STRIPE_PUBLISHABLE_KEY,
           "currency": STRIPE_CURRENCY, "amount_cents": str(amount_minor), "ui_mode": "hosted"}
    return {k: v for k, v in out.items() if v}


def stripe_get_session(session_id: str) -> dict:
    r = requests.get(f"{STRIPE_API}/checkout/sessions/{session_id}", auth=(STRIPE_SECRET_KEY, ""), timeout=20)
    body = r.json()
    if r.status_code >= 400:
        raise RuntimeError(body.get("error", {}).get("message", f"Stripe error {r.status_code}"))
    return body


def agent_wallet_address():
    try:
        return str(agent.wallet.address())
    except Exception:
        return None


def _ledger():
    from cosmpy.aerial.client import LedgerClient, NetworkConfig
    return LedgerClient(NetworkConfig.fetchai_stable_testnet() if FET_USE_TESTNET else NetworkConfig.fetchai_mainnet())


def fet_balance(address: str) -> int:
    from cosmpy.crypto.address import Address
    return int(_ledger().query_bank_balance(Address(address), denom=FET_DENOM))


def fet_micro(amount_fet: str) -> int:
    return int(round(float(amount_fet) * 10 ** 18))


def verify_fet_tx(tx_hash: str, recipient: str, amount_fet: str, sender_wallet: str = None) -> list:
    """On-chain check (as in Fetch.ai's fet_direct example): successful tx, transfer to our wallet, enough FET."""
    try:
        tx = _ledger().query_tx(tx_hash)
    except Exception as e:
        return [f"transaction not found on the Fetch.ai {'testnet' if FET_USE_TESTNET else 'mainnet'} ({str(e)[:80]})"]
    if not tx.is_successful():
        return ["transaction failed on-chain"]
    ok_recipient = ok_amount = ok_sender = False
    for etype, attrs in (tx.events or {}).items():
        if etype != "transfer" or attrs.get("recipient") != recipient:
            continue
        ok_recipient = True
        ok_sender = ok_sender or not sender_wallet or attrs.get("sender") == sender_wallet
        amt = str(attrs.get("amount", ""))
        if amt.endswith(FET_DENOM):
            try:
                ok_amount = ok_amount or int(amt[: -len(FET_DENOM)]) >= fet_micro(amount_fet)
            except ValueError:
                pass
    problems = []
    if not ok_recipient:
        problems.append("no transfer to the MediTwin wallet in this transaction")
    if ok_recipient and not ok_amount:
        problems.append(f"amount is below {amount_fet} FET")
    if ok_recipient and not ok_sender:
        problems.append("sender wallet does not match")
    return problems


def check_session(p, s) -> list:
    """Problems that block a receipt: wrong session, amount or PO, or not paid yet."""
    problems = []
    if not p:
        problems.append("no matching pending payment")
    else:
        if s.get("id") != p["checkout"]["id"]:
            problems.append("checkout session does not match this request")
        if int(s.get("amount_total") or 0) != int(p["amount_minor"]):
            problems.append("amount does not match the request")
        if (s.get("metadata") or {}).get("po") != p["po"]:
            problems.append("PO does not match the request")
    if s.get("payment_status") != "paid":
        problems.append("Stripe shows the payment as not completed yet")
    return problems


def record_receipt(ctx: Context, sender: str, p: dict, tx: str) -> str:
    receipt = {"po": p["po"], "supplier": p["supplier"] or "supplier", "amount_inr": p["amount_inr"],
               "pct": p["pct"], "transaction_id": tx,
               "paid_at": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")}
    ctx.storage.set(f"tx_{tx}", receipt)
    for key in (f"receipts_{sender}", "receipts_all"):
        ctx.storage.set(key, ((ctx.storage.get(key) or []) + [receipt])[-20:])
    close_pending(ctx, p, "paid", sender)
    balance = p["po_total_inr"] - p["amount_inr"]
    if p.get("method") == "fet":
        net = "Fetch.ai testnet" if FET_USE_TESTNET else "Fetch.ai mainnet"
        return (f"**Payment received** (verified on the {net})\n"
                f"- PO: {p['po']}\n- Supplier: {receipt['supplier']}\n"
                f"- Advance: {inr(p['amount_inr'])} ({p['pct']:g}% of {inr(p['po_total_inr'])}), settled as "
                f"{p['fet_amount']} FET (demo amount) to `{p['wallet']}`\n"
                f"- Balance due on delivery: {inr(balance)}\n- Proof: `{tx}`\n\n"
                f"Supplier notified to confirm dispatch (simulated). _Synthetic data; testnet FET has no real value._")
    return (f"**Payment received** (verified with Stripe, test mode)\n"
            f"- PO: {p['po']}\n- Supplier: {receipt['supplier']}\n"
            f"- Advance: {inr(p['amount_inr'])} ({p['pct']:g}% of {inr(p['po_total_inr'])}), charged "
            f"{p['currency']} {p['display_amount']}\n"
            f"- Balance due on delivery: {inr(balance)}\n- Stripe session: `{tx}`\n\n"
            f"Supplier notified to confirm dispatch (simulated). _Synthetic data; no real money moved._")


def parse_card_action(text: str):
    try:
        j = json.loads(text)
        if isinstance(j, dict) and j.get("action") in ("verify_payment", "cancel_payment"):
            return j["action"], j.get("reference")
    except (ValueError, TypeError):
        pass
    low = text.lower()
    for act in ("verify_payment", "cancel_payment"):
        if act in low:
            ref = re.search(r"po-\d{8}-[a-z0-9]{4}-[a-z0-9]{6}", low)
            return act, ref.group(0).upper() if ref else None
    return None, None


def payment_request(p: dict) -> RequestPayment:
    if p.get("method") == "fet":
        return RequestPayment(
            accepted_funds=[Funds(currency="FET", amount=p["fet_amount"], payment_method="fet_direct")],
            recipient=p["wallet"],
            deadline_seconds=DEADLINE_SECONDS,
            reference=p["reference"],
            description=f"{p['pct']:g}% advance ({inr(p['amount_inr'])}) for {p['po']}: {p['fet_amount']} FET demo settlement",
            metadata={"fet_network": "stable-testnet" if FET_USE_TESTNET else "mainnet",
                      "mainnet": "false" if FET_USE_TESTNET else "true",
                      "provider_agent_wallet": p["wallet"], "service": "meditwin_supplier_advance",
                      "po": p["po"], "content": f"Pay {p['fet_amount']} FET to settle the {p['po']} advance."},
        )
    return RequestPayment(
        accepted_funds=[Funds(currency=p["currency"], amount=p["display_amount"], payment_method="stripe")],
        recipient=p["recipient"],
        deadline_seconds=DEADLINE_SECONDS,
        reference=p["reference"],
        description=f"{p['pct']:g}% advance ({inr(p['amount_inr'])}) for {p['po']} to {p['supplier'] or 'supplier'}",
        metadata={"stripe": p["checkout"], "service": "meditwin_supplier_advance", "po": p["po"]},
    )


# ---------------- pending-payment state ----------------
def save_pending(ctx: Context, sender: str, p: dict):
    ctx.storage.set(f"pay_{p['reference']}", p)
    ctx.storage.set(f"pending_{sender}", p["reference"])
    ctx.storage.set("pending_latest", {"reference": p["reference"], "t": time.time()})


def load_pending(ctx: Context, sender: str, reference: str = None):
    """By reference first (echoed in CommitPayment), then by sender, then the latest one: ASI:One may use a
    different session address for the payment card than for the chat."""
    refs = [reference, ctx.storage.get(f"pending_{sender}"), (ctx.storage.get("pending_latest") or {}).get("reference")]
    for ref in refs:
        if not ref:
            continue
        p = ctx.storage.get(f"pay_{ref}")
        if p and p.get("status") == "pending" and time.time() - p.get("t", 0) < PENDING_TTL:
            return p
    return None


def close_pending(ctx: Context, p: dict, status: str, sender: str = None):
    p["status"] = status
    ctx.storage.set(f"pay_{p['reference']}", p)
    for key in (f"pending_{sender}" if sender else None, f"pending_{p.get('user')}"):
        if key:
            ctx.storage.set(key, None)
    latest = ctx.storage.get("pending_latest") or {}
    if latest.get("reference") == p["reference"]:
        ctx.storage.set("pending_latest", None)


# ---------------- chat ----------------
@agent.on_event("startup")
async def on_start(ctx: Context):
    problem = stripe_ready()
    ctx.logger.info(f"MediTwin Payment Agent online: {ctx.agent.address} | default={DEFAULT_PAY_METHOD} | "
                    f"FET wallet={agent_wallet_address()} ({'testnet' if FET_USE_TESTNET else 'mainnet'}) | stripe="
                    f"{'ready (test mode)' if not problem else problem}")


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(getattr(c, "text", "") or "" for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    text = strip_mentions(text) or text
    t = text.lower()
    ctx.logger.info(f"Chat from {sender[:12]}: {text[:200]}")
    try:
        await handle_chat(ctx, sender, text, t)
    except Exception as e:
        ctx.logger.error(f"Payment chat error: {e}")
        await say(ctx, sender, f"Sorry, the payment step failed ({type(e).__name__}: {str(e)[:150]}). No money was taken.")


def fet_instructions(p: dict, still_pending: bool = False) -> str:
    net = "Fetch.ai testnet" if FET_USE_TESTNET else "Fetch.ai mainnet"
    head = (f"Payment for {p['po']} is still pending." if still_pending else
            f"Requesting a **{p['pct']:g}% advance of {inr(p['amount_inr'])}** for **{p['po']}**"
            f"{' to ' + p['supplier'] if p['supplier'] else ''} (PO total {inr(p['po_total_inr'])}), "
            f"settled in **FET** on the {net} as a **{p['fet_amount']} FET** demo payment.")
    return (f"{head}\n\n"
            f"1. Approve the FET payment card if your client shows one, or send **{p['fet_amount']} FET** "
            f"({'testnet' if FET_USE_TESTNET else 'mainnet'}) from any Fetch wallet to:\n`{p['wallet']}`\n"
            f"2. Press **I've paid, verify**, or type `verify tx <transaction hash>`.\n\n"
            f"I check the transfer on-chain before issuing a receipt. "
            f"_{'Testnet FET has no real value.' if FET_USE_TESTNET else 'Mainnet: real FET.'}_")


async def verify_fet_pending(ctx: Context, sender: str, p: dict, text: str, buyer_wallet: str = None):
    """Verify by transaction hash when given, otherwise by the wallet balance increase since the request."""
    m = re.search(r"\b([A-Fa-f0-9]{64})\b", text or "")
    if m:
        tx = m.group(1).upper()
        if ctx.storage.get(f"tx_{tx}"):
            await say(ctx, sender, "That transaction was already used for a receipt.")
            return
        problems = await asyncio.to_thread(verify_fet_tx, tx, p["wallet"], p["fet_amount"], buyer_wallet)
        proof = tx
    else:
        problems, proof = [], None
        try:
            now = await asyncio.to_thread(fet_balance, p["wallet"])
            if p.get("start_balance") is None:
                problems.append("could not read the starting balance; please send the transaction hash")
            elif now - int(p["start_balance"]) < fet_micro(p["fet_amount"]):
                problems.append(f"no new {p['fet_amount']} FET received yet")
            else:
                proof = f"wallet balance +{(now - int(p['start_balance'])) / 10 ** 18:g} FET"
        except Exception as e:
            problems.append(f"could not reach the Fetch.ai ledger ({str(e)[:80]})")
    if problems:
        await say(ctx, sender, f"Not verified yet: {'; '.join(problems)}.\n\n" + fet_instructions(p, still_pending=True),
                  payment_card(p))
        return
    await say(ctx, sender, record_receipt(ctx, sender, p, proof))


async def handle_chat(ctx: Context, sender: str, text: str, t: str):
    action, ref = parse_card_action(text)
    pending = load_pending(ctx, sender, ref)

    if action == "verify_payment" or (pending and re.search(r"\b(verify|i paid|i've paid|ive paid|done|completed)\b", t)):
        if not pending:
            await say(ctx, sender, "There is no pending payment to verify. Say `payment status` to see receipts.")
            return
        if pending.get("method") == "fet":
            await verify_fet_pending(ctx, sender, pending, text)
            return
        s = await asyncio.to_thread(stripe_get_session, pending["checkout"]["id"])
        problems = check_session(pending, s)
        if problems:
            await say(ctx, sender, f"Not verified yet: {'; '.join(problems)}.\n[Open the Stripe checkout]"
                                   f"({pending['checkout'].get('url', '')}) and pay with 4242 4242 4242 4242, "
                                   "then press verify again.", payment_card(pending))
            return
        await say(ctx, sender, record_receipt(ctx, sender, pending, s["id"]))
        return

    if action == "cancel_payment":
        t = "cancel payment"

    if re.search(r"\b(cancel|stop|abort)\b", t):
        if pending:
            close_pending(ctx, pending, "cancelled", sender)
            await say(ctx, sender, f"Payment request for {pending['po']} cancelled. No money was taken.")
        else:
            await say(ctx, sender, "There is no pending payment to cancel.")
        return

    if re.search(r"\b(status|receipts?|history|paid)\b", t) and not re.search(r"\bpo-\d{8}", t):
        receipts = (ctx.storage.get(f"receipts_{sender}") or []) + (ctx.storage.get("receipts_all") or [])
        seen, lines = set(), []
        for r in reversed(receipts):
            if r["transaction_id"] in seen:
                continue
            seen.add(r["transaction_id"])
            lines.append(f"- {r['po']}: {inr(r['amount_inr'])} to {r['supplier']} (paid {r['paid_at']})")
        msg = "**Payments**\n" + ("\n".join(lines[:5]) if lines else "No payments yet.")
        if pending:
            msg += f"\n\nPending: {inr(pending['amount_inr'])} for {pending['po']} (complete the payment card above)."
        await say(ctx, sender, msg)
        return

    req = parse_payment_request(text)
    wants_pay = bool(re.search(r"\b(pay|payment|advance|settle)\b", t))

    if pending and (not wants_pay or req["po"] in (None, pending["po"])) and pending.get("method") == "fet":
        await ctx.send(sender, payment_request(pending))
        await say(ctx, sender, fet_instructions(pending, still_pending=True), payment_card(pending))
        return

    if pending and (not wants_pay or req["po"] in (None, pending["po"])):
        await say(ctx, sender, f"Payment of {inr(pending['amount_inr'])} for {pending['po']} is still pending.\n"
                               f"[Pay {pending['currency']} {pending['display_amount']} on Stripe (test mode)]"
                               f"({pending['checkout'].get('url', '')}), then press **I've paid, verify**. "
                               "Say `cancel payment` to stop.", payment_card(pending))
        return

    if not wants_pay:
        await say(ctx, sender, HELP)
        return

    if not req["po"] or not req["po_total_inr"]:
        await say(ctx, sender, "I need the PO number and its total. Paste it like this:\n"
                               "`pay 10% advance for PO-20261024-373E, Himalaya MedSupply, total Rs 361,920`")
        return

    amount_inr = round(req["po_total_inr"] * req["pct"] / 100.0, 2)
    if amount_inr > MAX_PAYMENT_INR:
        await say(ctx, sender, f"{inr(amount_inr)} is above this demo's cap of {inr(MAX_PAYMENT_INR)} per payment. "
                               "Try a smaller advance, e.g. 10%.")
        return

    method = "stripe" if re.search(r"\b(card|stripe|usd)\b", t) else (
        "fet" if re.search(r"\bfet\b", t) else DEFAULT_PAY_METHOD)
    if method == "fet":
        wallet = agent_wallet_address()
        if not wallet:
            await say(ctx, sender, "FET payments are unavailable (agent wallet not found). Say `pay ... by card` to use Stripe.")
            return
        try:
            start_balance = await asyncio.to_thread(fet_balance, wallet)
        except Exception as e:
            ctx.logger.warning(f"Could not read wallet balance: {e}")
            start_balance = None
        reference = f"{req['po']}-{uuid4().hex[:6].upper()}"
        p = {"reference": reference, "method": "fet", "po": req["po"], "supplier": req["supplier"],
             "pct": req["pct"], "po_total_inr": req["po_total_inr"], "amount_inr": amount_inr,
             "fet_amount": FET_AMOUNT, "wallet": wallet, "start_balance": start_balance,
             "recipient": wallet, "user": sender, "status": "pending", "t": time.time()}
        save_pending(ctx, sender, p)
        ctx.logger.info(f"RequestPayment {reference}: {FET_AMOUNT} FET to {wallet} for {req['po']}")
        await ctx.send(sender, payment_request(p))   # fet_direct request: ASI:One can render its wallet card
        await say(ctx, sender, fet_instructions(p), payment_card(p))
        return

    problem = stripe_ready()
    if problem:
        await say(ctx, sender, problem)
        return
    minor, currency, display = to_minor_units(amount_inr)
    reference = f"{req['po']}-{uuid4().hex[:6].upper()}"
    checkout = await asyncio.to_thread(
        stripe_create_session, amount_minor=minor, po=req["po"], supplier=req["supplier"],
        pct=req["pct"], reference=reference, user=sender)
    p = {"reference": reference, "po": req["po"], "supplier": req["supplier"], "pct": req["pct"],
         "po_total_inr": req["po_total_inr"], "amount_inr": amount_inr, "amount_minor": minor,
         "currency": currency, "display_amount": display, "checkout": checkout,
         "recipient": str(ctx.agent.address), "user": sender, "status": "pending", "t": time.time()}
    save_pending(ctx, sender, p)
    ctx.logger.info(f"RequestPayment {reference}: {currency} {display} for {req['po']}")
    await ctx.send(sender, payment_request(p))   # Agent Payment Protocol request (rendered by clients that support it)
    usd_note = f" (about USD {display})" if currency == "USD" else ""
    await say(ctx, sender,
              f"Requesting a **{req['pct']:g}% advance of {inr(amount_inr)}**{usd_note} for **{req['po']}**"
              f"{' to ' + req['supplier'] if req['supplier'] else ''} (PO total {inr(req['po_total_inr'])}).\n\n"
              f"1. [Pay on Stripe (test mode)]({checkout.get('url', '')}) with card 4242 4242 4242 4242, "
              "any future date, any CVC.\n2. Come back and press **I've paid, verify** (or type `verify`).\n\n"
              "I check the payment with Stripe before issuing a receipt. _Test mode: no real money moves._",
              payment_card(p))


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    return


# ---------------- payment protocol (seller) ----------------
@payment_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    ctx.logger.info(f"CommitPayment from {sender[:12]}: tx={msg.transaction_id} ref={msg.reference}")
    if msg.funds.payment_method == "fet_direct" and msg.transaction_id:
        p = load_pending(ctx, sender, msg.reference)
        if not p or p.get("method") != "fet":
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="no matching pending FET payment"))
            return
        buyer = (msg.metadata or {}).get("buyer_fet_wallet") or (msg.metadata or {}).get("buyer_fet_address")
        problems = await asyncio.to_thread(verify_fet_tx, msg.transaction_id, p["wallet"], p["fet_amount"], buyer)
        if problems:
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="; ".join(problems)))
            await say(ctx, sender, f"Payment not accepted: {'; '.join(problems)}.")
            return
        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
        await say(ctx, sender, record_receipt(ctx, sender, p, msg.transaction_id))
        return
    if msg.funds.payment_method != "stripe" or not msg.transaction_id:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id,
                                             reason="Unsupported payment method (expected stripe)."))
        return
    done = ctx.storage.get(f"tx_{msg.transaction_id}")
    if done:  # idempotent: the same payment approved twice
        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
        await say(ctx, sender, f"Already received: {inr(done['amount_inr'])} for {done['po']}.")
        return
    p = load_pending(ctx, sender, msg.reference)
    try:
        s = await asyncio.to_thread(stripe_get_session, msg.transaction_id)
    except Exception as e:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason=f"Could not verify with Stripe: {e}"))
        return
    meta = s.get("metadata") or {}
    if not p:  # recover by the reference stored on the Stripe session itself
        cand = ctx.storage.get(f"pay_{meta.get('reference', '')}")
        p = cand if cand and cand.get("status") == "pending" else None
    problems = check_session(p, s)
    if problems:
        reason = "; ".join(problems)
        ctx.logger.warning(f"Payment not accepted: {reason}")
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason=reason))
        await say(ctx, sender, f"Payment not accepted: {reason}. No receipt issued."
                               + (" Finish the Stripe checkout, then approve again." if p else ""))
        return

    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    await say(ctx, sender, record_receipt(ctx, sender, p, msg.transaction_id))


@payment_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    p = load_pending(ctx, sender)
    if p:
        close_pending(ctx, p, "rejected", sender)
    await say(ctx, sender, f"Payment declined{': ' + msg.reason if msg.reason else ''}. No money was taken.")


agent.include(chat_proto, publish_manifest=True)
agent.include(payment_proto, publish_manifest=True)