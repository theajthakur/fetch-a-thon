"""Offline check of src/agents/payment-agent.py: real uagents models, mocked Stripe API (no keys, no network).
Needs: pip install uagents==0.26.1 uagents-core==0.4.12 requests.  Run: python tools/check_payment_agent.py"""
import asyncio, importlib.util, json, os
os.environ.update(STRIPE_SECRET_KEY="sk_test_x", STRIPE_PUBLISHABLE_KEY="pk_test_x")
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
spec = importlib.util.spec_from_file_location(
    "pay", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "agents", "payment-agent.py"))
P = importlib.util.module_from_spec(spec); spec.loader.exec_module(P)

SESSIONS = {}
class Resp:
    def __init__(s, code, body): s.status_code, s._b = code, body
    def json(s): return s._b
def fake_post(url, data=None, auth=None, timeout=None):
    sid = f"cs_test_{len(SESSIONS)+1}"
    SESSIONS[sid] = {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}", "payment_status": "unpaid",
                     "amount_total": int(data["line_items[0][price_data][unit_amount]"]),
                     "metadata": {"po": data["metadata[po]"], "reference": data["metadata[reference]"]}}
    return Resp(200, SESSIONS[sid])
def fake_get(url, auth=None, timeout=None):
    return Resp(200, SESSIONS[url.rsplit("/", 1)[1]])
P.requests.post, P.requests.get = fake_post, fake_get

class Store(dict):
    def get(s, k): return dict.get(s, k)
    def set(s, k, v): s[k] = v
class Log:
    def info(s, x): pass
    warning = error = info
sent = []
class Ctx:
    storage, logger = Store(), Log()
    class agent: address = "agent1payment"
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
    m = await chat("userA", "@meditwin-payment pay 10% advance for PO-20261024-373E, Himalaya MedSupply, total Rs 361,920")
    req = next(x for _, x in sent if isinstance(x, P.RequestPayment))
    print("RequestPayment:", req.accepted_funds[0].currency, req.accepted_funds[0].amount)
    print("link in reply:", "checkout.stripe.com" in m.content[0].text,
          "| card:", len(m.content) > 1 and json.loads(m.content[1].metadata["card_payload"])["approve_cta"]["label"])
    sid = req.metadata["stripe"]["id"]; ref = req.reference

    m = await chat("userB", json.dumps({"action": "verify_payment", "reference": ref}))
    print("verify before paying ->", m.content[0].text.splitlines()[0][:70])

    SESSIONS[sid]["payment_status"] = "paid"
    m = await chat("userB", json.dumps({"action": "verify_payment", "reference": ref}))
    print(m.content[0].text)

    m = await chat("userA", "payment status"); print(m.content[0].text)
    m = await chat("userA", "pay 10% advance for PO-20261024-AAAA, total Rs 10,000")
    sid2 = next(x for _, x in sent if isinstance(x, P.RequestPayment)).metadata["stripe"]["id"]
    m = await chat("userA", json.dumps({"action": "cancel_payment"})); print(m.content[0].text)

    # protocol path still works when a client renders the native payment card
    m = await chat("userA", "pay 5% advance for PO-20261024-BBBB, total Rs 20,000")
    req3 = next(x for _, x in sent if isinstance(x, P.RequestPayment))
    SESSIONS[req3.metadata["stripe"]["id"]]["payment_status"] = "paid"
    sent.clear()
    await P.on_commit(ctx, "x", P.CommitPayment(funds=req3.accepted_funds[0], recipient=req3.recipient,
                                                 transaction_id=req3.metadata["stripe"]["id"], reference=req3.reference))
    print("CommitPayment ->", type(sent[0][1]).__name__)

asyncio.run(main())
