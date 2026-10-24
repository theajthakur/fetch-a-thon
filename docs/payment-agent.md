# Payment Agent (`meditwin-payment`)

The Payment Agent collects the supplier advance for an approved purchase order through the **Agent Payment Protocol** (seller role). It is a separate hosted agent: the Action Agent hands the payment step to it, and it can also be used directly via `@meditwin-payment`.

| | |
|---|---|
| Agent address | `agent1qfx0vc5d6gqg7dcxqc3qm0qh47slda53khgyhvst03tyye32zq9gzv5tv47` |
| FET wallet (Dorado testnet) | `fetch1r9tjvdkq32hf93wpxna6rkd6q5eal82uplgdgf` |
| Code | [`src/agents/payment-agent.py`](../src/agents/payment-agent.py) |
| Offline checks | [`tools/check_payment_agent.py`](../tools/check_payment_agent.py) |
| Live testnet sender | [`tools/send_test_fet.py`](../tools/send_test_fet.py) |

## Protocol messages

| Direction | Message | When |
|---|---|---|
| Agent → user | `RequestPayment` | A payment is requested: `Funds(currency="FET", amount=FET_AMOUNT, payment_method="fet_direct")` to the agent wallet, or `Funds(currency="USD", payment_method="stripe")` with Stripe checkout metadata |
| User → agent | `CommitPayment` | The user's client paid (for FET: transaction hash, optionally `buyer_fet_wallet` in metadata) |
| User → agent | `RejectPayment` | The user declined; the pending payment is closed |
| Agent → user | `CompletePayment` | The payment was verified |
| Agent → user | `CancelPayment` | Verification failed, with the reason |

`RequestPayment.metadata` only carries string values. For FET it contains `fet_network` (`stable-testnet`), `mainnet` (`false`), `provider_agent_wallet`, `po` and `content`, the shape ASI:One uses to render its native FET payment card.

## Flow from the Action Agent

1. After a PO is approved, the Action Agent sends a detail card: **Pay with FET (Fetch.ai testnet)** or **Card (Stripe test mode)**, then **Pay 10% advance** or **Skip for now**.
2. On **Pay**, the Action Agent sends `pay 10% advance for <PO>, <supplier>, total Rs <supplier total> in FET` (or `by card`) to the Payment Agent and relays its replies back to the user, labelled "Payment Agent:".
3. The Payment Agent replies with the amount, the wallet address (FET) or Stripe link (card), and a review card: **I've paid, verify** / **Cancel**.
4. Verification and receipt (below). Clicks and typed commands (`verify`, `verify tx <hash>`, `cancel payment`, `payment status`) all work through the Action Agent.

## Verification

| Method | Check before a receipt is issued |
|---|---|
| FET, `CommitPayment` or `verify tx <hash>` | The transaction succeeded on the Dorado testnet, it contains a transfer to the agent wallet, and the amount is at least `FET_AMOUNT` (in `atestfet`); the sender must match `buyer_fet_wallet` when provided. Each hash can be used for one receipt only. |
| FET, `verify` without a hash | The agent wallet balance rose by at least `FET_AMOUNT` since the request (assumes one payment at a time; the hash check is the strict one). |
| Card (Stripe) | The checkout session is `paid`, `amount_total` equals the requested amount, and the session's `po` metadata matches. |

The receipt shows the PO, supplier, advance in rupees (10% of the supplier total), what was charged, the balance due on delivery and the proof (transaction hash, balance change or Stripe session ID).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEFAULT_PAY_METHOD` | `fet` | Used when the request does not say `in FET` or `by card` |
| `FET_AMOUNT` | `0.1` | Demo settlement amount in FET (testnet FET has no value) |
| `FET_USE_TESTNET` | `true` | Dorado testnet (`atestfet`) or mainnet (`afet`) |
| `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | none | Stripe **test** keys; live keys are refused |
| `STRIPE_CURRENCY` | `usd` | Stripe currency (INR amounts are converted with `INR_PER_USD`, default 84) |
| `STRIPE_SUCCESS_URL` | Agentverse page | Where Stripe returns after payment, e.g. `https://asi1.ai/` |

On the Action Agent, `PAYMENT_ADDR` enables the in-chat payment step; without it, the PO shows the two commands to send to `@meditwin-payment` instead.

## Safeguards

- No receipt without independent verification (ledger or Stripe).
- Transaction hashes are single-use; duplicate approvals do not double-count.
- Stripe test keys only; per-payment cap of Rs 500,000.
- Pending payments expire after 30 minutes and can be cancelled at any time.
- Pending payments are found by reference, sender or latest, because ASI:One can change session addresses between messages.