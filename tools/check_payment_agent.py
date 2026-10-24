"""Offline check of src/agents/payment-agent.py: real uagents models, mocked Stripe & Fetch ledger (no keys, no network).
Needs: pip install uagents==0.26.1 uagents-core==0.4.12 requests.  Run: python tools/check_payment_agent.py"""
import asyncio
import importlib.util
import json
import os

os.environ.update(STRIPE_SECRET_KEY="sk_test_x", STRIPE_PUBLISHABLE_KEY="pk_test_x")
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

spec = importlib.util.spec_from_file_location(
    "pay", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "agents", "payment-agent.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

# --- Mock Stripe ---
SESSIONS = {}
class Resp:
    def __init__(s, code, body): s.status_code, s._b = code, body
    def json(s): return s._b

def fake_post(url, data=None, auth=None, timeout=None):
    sid = f"cs_test_{len(SESSIONS)+1}"
    SESSIONS[sid] = {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}", "payment_status": "unpaid",
                     "amount_total": int(data.get("line_items[0][price_data][unit_amount]", 0)),
                     "metadata": {"po": data.get("metadata[po]", ""), "reference": data.get("metadata[reference]", "")}}
    return Resp(200, SESSIONS[sid])

def fake_get(url, auth=None, timeout=None):
    return Resp(200, SESSIONS[url.rsplit("/", 1)[1]])

P.requests.post, P.requests.get = fake_post, fake_get

# --- Mock Fetch ledger for FET ---
MOCK_TXS = {}
MOCK_BALANCES = {}

def fake_fet_balance(address: str) -> int:
    return MOCK_BALANCES.get(address, 0)

def fake_verify_fet_tx(tx_hash: str, recipient: str, amount_fet: str, sender_wallet: str = None) -> list:
    if tx_hash not in MOCK_TXS:
        return ["transaction not found on the Fetch.ai testnet"]
    tx = MOCK_TXS[tx_hash]
    if tx.get("recipient") != recipient:
        return ["no transfer to the MediTwin wallet in this transaction"]
    if float(tx.get("amount_fet", "0")) < float(amount_fet):
        return [f"amount is below {amount_fet} FET"]
    return []

P.fet_balance = fake_fet_balance
P.verify_fet_tx = fake_verify_fet_tx

class Store(dict):
    def get(s, k): return dict.get(s, k)
    def set(s, k, v): s[k] = v

class Log:
    def info(s, x): pass
    warning = error = info

sent = []
class Ctx:
    storage, logger = Store(), Log()
    class agent:
        address = "agent1payment"
        wallet = type("W", (), {"address": lambda: "fetch1r9tjvdkq32hf93wpxna6rkd6q5eal82uplgdgf"})()
    async def send(s, to, m): sent.append((to, m))

ctx = Ctx()

def last_chat():
    return next(m for _, m in reversed(sent) if isinstance(m, P.ChatMessage))

async def chat(sender, text):
    sent.clear()
    await P.on_chat(ctx, sender, P.ChatMessage(timestamp=P.datetime.now(P.timezone.utc), msg_id=P.uuid4(),
                                               content=[P.TextContent(type="text", text=text)]))
    return last_chat()

async def main():
    # 1. Default flow: FET payment request
    m = await chat("userA", "@meditwin-payment pay 10% advance for PO-20261024-373E, Himalaya MedSupply, total Rs 361,920")
    req = next(x for _, x in sent if isinstance(x, P.RequestPayment))
    print("RequestPayment (FET):", req.accepted_funds[0].currency, req.accepted_funds[0].amount)
    assert req.accepted_funds[0].currency == "FET"
    ref = req.reference

    # 2. Verify before paying -> refused
    m = await chat("userB", json.dumps({"action": "verify_payment", "reference": ref}))
    print("verify before paying (FET) ->", m.content[0].text.splitlines()[0][:70])
    assert "Balance not received" in m.content[0].text or "Not verified yet" in m.content[0].text

    # 3. Verify with on-chain transaction hash
    tx_hash = "A" * 64
    MOCK_TXS[tx_hash] = {"recipient": P.agent_wallet_address(), "amount_fet": "0.1"}
    m = await chat("userA", f"verify tx {tx_hash}")
    print("verify with tx hash ->", m.content[0].text.splitlines()[0])
    assert "Payment received" in m.content[0].text

    # 4. Replay transaction hash on a new pending payment -> refused
    await chat("userA", "pay 10% advance for PO-20261024-REPL, Himalaya MedSupply, total Rs 100,000 in FET")
    m = await chat("userA", f"verify tx {tx_hash}")
    print("replay tx hash ->", m.content[0].text.splitlines()[0])
    assert "already used" in m.content[0].text

    # 5. Card flow (Stripe)
    m = await chat("userA", "pay 10% advance for PO-20261024-CARD, Himalaya MedSupply, total Rs 100,000 by card")
    req_stripe = next(x for _, x in sent if isinstance(x, P.RequestPayment))
    print("RequestPayment (Card):", req_stripe.accepted_funds[0].currency, req_stripe.accepted_funds[0].amount)
    assert req_stripe.accepted_funds[0].currency == "USD"
    sid = req_stripe.metadata["stripe"]["id"]

    # 6. Verify card payment after simulated Stripe success
    SESSIONS[sid]["payment_status"] = "paid"
    m = await chat("userA", json.dumps({"action": "verify_payment", "reference": req_stripe.reference}))
    print("verify card ->", m.content[0].text.splitlines()[0])
    assert "Payment received" in m.content[0].text

    # 7. Cancel payment
    await chat("userA", "pay 10% advance for PO-20261024-CANC, total Rs 10,000 in FET")
    m = await chat("userA", json.dumps({"action": "cancel_payment"}))
    print("cancel payment ->", m.content[0].text.splitlines()[0])
    assert "cancelled" in m.content[0].text

    # 8. Protocol CommitPayment -> CompletePayment
    await chat("userA", "pay 5% advance for PO-20261024-COMM, total Rs 20,000 by card")
    req_comm = next(x for _, x in sent if isinstance(x, P.RequestPayment))
    SESSIONS[req_comm.metadata["stripe"]["id"]]["payment_status"] = "paid"
    sent.clear()
    await P.on_commit(ctx, "x", P.CommitPayment(funds=req_comm.accepted_funds[0], recipient=req_comm.recipient,
                                                 transaction_id=req_comm.metadata["stripe"]["id"], reference=req_comm.reference))
    print("CommitPayment ->", type(sent[0][1]).__name__)
    assert type(sent[0][1]).__name__ == "CompletePayment"

    print("\nAll payment flow checks passed!")

asyncio.run(main())
