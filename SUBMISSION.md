# Devpost submission: copy/paste texts

> Fields in the order Devpost asks for them. Replace `<...>` with the real links before submitting.

## Project name
Tendero

## Elevator pitch (max 200 chars)
An AI sales agent for chat-first small shops: it sells, sends a PayPal checkout, and only says "paid" after PayPal's signed webhook is verified and reconciled.

## Built with (tags)
python, fastapi, paypal, paypal-orders-v2, paypal-webhooks, claude, anthropic, ag-grid, sqlite, playwright, javascript

## Links
- Code repository: `https://github.com/<user>/tendero`
- Demo video (YouTube, public): `https://youtu.be/<id>`
- Try it: run locally in 5 minutes, instructions in the README (works fully offline; add sandbox keys for the real PayPal sandbox)

---

## About the project (Story, markdown)

### Inspiration
Across Latin America, a huge number of small businesses sell mostly through chat: workshops, bakeries, makers who ship abroad. I've spent ~10 years in tech and I integrate PayPal and Mercado Pago with verified webhooks on a real platform, so I see the same pattern every week. The owner answers every message by hand, types totals from memory, sends a payment link, and then opens the PayPal app trying to work out which payment belongs to which chat. Sales are lost at night, totals get mistyped, and "I already paid" screenshots are trusted because checking each one takes too long.

Most AI shopping agents stop at "recommend" or "checkout". The hard part for a small merchant is **after** the click: knowing the money is real, that it matches the order, and what they actually keep after fees.

### What it does
Tendero puts an agent in the shop's chat and closes the whole loop: **sell → collect → verify → reconcile → notify**.

- **Customer agent.** Searches the catalog, builds the cart (prices and shipping come from the server), and asks the customer to confirm the total. Then it creates a **PayPal Orders v2** checkout and drops a *Pay with PayPal* button in the chat.
- **Verified payments.** After approval we capture. Every PayPal webhook is checked with **`/v1/notifications/verify-webhook-signature`**, and `PAYMENT.CAPTURE.COMPLETED` is reconciled against the order (status, amount, currency, `custom_id`). The PayPal fee and net come from `seller_receivable_breakdown`. Only then does the customer get "Payment confirmed by PayPal", and that message is deterministic, not generated.
- **Owner console.** Live AG Grid ledger, KPIs (collected, fees, net, awaiting, rejected webhooks) and a payment timeline that shows each step.
- **Owner copilot.** "How did we do today?" runs the reconciliation sweep and answers with real numbers. "Refund the last order, it arrived cracked" finds the order, issues a **Payments v2 refund**, and notifies the customer when the signed `PAYMENT.CAPTURE.REFUNDED` arrives.
- **Safety nets.** Forged webhooks are rejected and change nothing. Duplicates are ignored. If a webhook is lost, a sweep re-reads the capture from the PayPal API. If the buyer closes the tab before returning, `CHECKOUT.ORDER.APPROVED` triggers the capture.

### How I built it
- **Backend:** Python + FastAPI, with an async PayPal REST client (OAuth2 token cache, Orders v2 create/capture, Payments v2 get/refund, webhook verification, `PayPal-Request-Id` idempotency keys) and a SQLite ledger with an audit timeline.
- **AI:** a provider-agnostic tool-use loop. By default it runs **Claude (`claude-sonnet-5-5`)** through the official Anthropic SDK, with strict JSON-schema tools, low effort for chat latency and server-side refusal fallbacks. The same loop can run an offline "demo brain" that emits the same tool calls, so anyone can run the project with no keys.
- **Guardrails in code, not in the prompt:** the model never does arithmetic, it can't create a payment link in the same turn it priced the cart (the customer must see the total first), there is a per-order cap, orders belong to their conversation, and `PAID` is only set by a verified webhook or a direct PayPal API read.
- **Local PayPal sandbox simulator:** I wanted to build and test the full flow, including signed webhooks, before having sandbox credentials. So I wrote a small simulator that mirrors the documented endpoints, payloads and error names (`ORDER_NOT_APPROVED`, `ORDER_ALREADY_CAPTURED`…) and signs its webhooks. The production client is the same code; switching is `PAYPAL_MODE=sandbox`.
- **Tests:** 23 automated tests cover pricing, reconciliation rules, the PayPal client, signature failures, the full chat-to-verified-payment flow, forged/duplicate/lost webhooks, the closed-tab path, refunds, and the exact request Claude receives.
- **Demo video:** recorded by a script (Playwright headless browser + synthetic voice + ffmpeg), so the video shows the real app running end to end.

### Challenges I ran into
- **Making "paid" mean paid.** Return URLs, capture responses and webhooks arrive in different orders. I settled on one rule: only a signature-verified webhook (or a direct read of the capture from PayPal) can mark an order paid, and every handler is idempotent.
- **Keeping the LLM away from money.** The model is great at conversation and bad at being an accountant. Moving pricing, confirmation and status into tools with server-side checks made the agent both more useful and safer.
- **Lost webhooks and closed tabs.** These are real-world failure modes I've seen in production, so they get explicit recovery paths and tests.

### Accomplishments that I'm proud of
- An agent that actually transacts end to end (sell, charge, verify, reconcile, refund) with guardrails that hold regardless of which model is plugged in.
- Forged-webhook rejection you can trigger live from the console.
- A repo a judge can run offline in five minutes, with tests for every money path.

### What I learned
- The PayPal webhook signature verification API is the cleanest way to make agent-driven commerce trustworthy: the agent never has to "believe" anyone.
- Tool design matters more than prompt design for agents that move money.

### What's next for Tendero
- WhatsApp Business and Instagram adapters (the agent is already channel-agnostic).
- Pay Later and Venmo as funding sources, and Payouts to split revenue between makers.
- PayPal Agent Toolkit / MCP server as an alternative tool backend for the owner copilot.
- Multi-currency display for LatAm buyers with USD settlement.

---

## Testing instructions (for judges)
1. `git clone <repo> && cd tendero && python -m venv .venv && .venv/Scripts/activate` (macOS/Linux: `source .venv/bin/activate`)
2. `pip install -r requirements.txt && python -m tendero.main` → open http://localhost:8000
3. In the customer chat type: `Hi! Do you ship to Madrid? I'd like 2 mate gourds and a bombilla`, then `Yes, send me the link`, then click **Pay with PayPal** → **Approve & pay**.
4. Watch the timeline (capture → webhook verified → reconciled → customer notified). Click **Send forged webhook** to see it rejected.
5. In *Owner copilot*: `How did we do today? Any issues?` and `Refund the last order - the gourd arrived cracked`.
6. Optional: set `ANTHROPIC_API_KEY` to run the agents on Claude; set `PAYPAL_MODE=sandbox` + sandbox credentials to use the real PayPal sandbox (README → "Use the real PayPal sandbox").
7. `pip install -r requirements-dev.txt && pytest -q` → 23 passed.

No login needed. All data is fictional.

## AI tools used (Devpost field)
- Claude API (Anthropic), model `claude-sonnet-5-5`: runtime brain of the customer agent and the owner copilot (tool use).
- Claude Code (Anthropic): AI coding assistant used during development (code, tests, docs, video pipeline).
- Microsoft Edge neural text-to-speech via the open-source `edge-tts` package: voice-over of the demo video.

## Sponsor tools used
- AG Grid Community: owner's orders & reconciliation ledger.

## Prize categories this project fits
- Grand prizes; **Best Use of Agentic Commerce**; **Best Use of PayPal + AI**; Most Impactful; Best Demo Delivery.

## "Is this a new project?"
Yes, created from scratch during the submission period (first commit November 2026).
