# Devpost submission: copy/paste texts

> Fields in the order Devpost asks for them. Replace `<...>` with the real links before submitting.

## Project name
Tendero

## Elevator pitch (max 200 chars)
An AI sales agent for chat-first small shops: it sells, sends a PayPal checkout, and only says "paid" after PayPal's signed webhook is verified and reconciled.

## Built with (tags)
python, fastapi, paypal, paypal-orders-v2, paypal-webhooks, paypal-server-sdk, apimatic, claude, anthropic, ag-grid, ag-charts, bryntum, channel3, sqlite, playwright, javascript

## Links
- Code repository: `https://github.com/Julian-Genuario/tendero`
- Demo video (YouTube, public): `https://youtu.be/WJs3VghvRD4`
- Try it: run locally in 5 minutes, instructions in the README (works with no accounts; add sandbox keys for the real PayPal sandbox)

---

## About the project (Story, markdown)

### Inspiration
Across Latin America, a huge number of small businesses sell mostly through chat: workshops, bakeries, makers who ship abroad. I've spent ~10 years in tech and I integrate PayPal and Mercado Pago with verified webhooks on a real platform, so I see the same pattern every week. The owner answers every message by hand, types totals from memory, sends a payment link, and then opens the PayPal app trying to work out which payment belongs to which chat. Sales are lost at night, totals get mistyped, and "I already paid" screenshots are trusted because checking each one takes too long.

Most AI shopping agents stop at "recommend" or "checkout". The hard part for a small merchant is **after** the click: knowing the money is real, that it matches the order, what they actually keep after fees, and getting the parcel out on time.

### What it does
Tendero puts an agent in the shop's chat and closes the whole loop: **sell → collect → verify → reconcile → fulfill → notify**.

- **Customer agent.** Searches the catalog, builds the cart (prices and shipping come from the server), and asks the customer to confirm the total. Then it creates a **PayPal Orders v2** checkout and drops a *Pay with PayPal* button in the chat.
- **Verified payments.** After approval we capture. Every PayPal webhook is checked with **`/v1/notifications/verify-webhook-signature`**, and `PAYMENT.CAPTURE.COMPLETED` is reconciled against the order (status, amount, currency, `custom_id`). The PayPal fee and net come from `seller_receivable_breakdown`. Only then does the customer get "Payment confirmed by PayPal" with a planned dispatch day, and that message is deterministic, not generated.
- **Fulfillment board (Bryntum Scheduler).** A verified payment books the work in the workshop: quality check, packing and courier pickup, linked by dependencies, inside workshop hours, with pickups only when the courier comes. The owner can drag tasks; the server validates every move and refuses impossible ones. A refund takes the order off the board.
- **Owner console (AG Grid + AG Charts).** Live KPIs, a payment timeline, and tabs for the orders ledger, inventory (stock bars and a chart computed from verified payments only) and market prices.
- **Owner copilot.** "How did we do today?" runs the reconciliation sweep and answers with real numbers. "Show me unpaid orders over $20" filters and sorts the AG Grid ledger on screen. "What do we ship next?" reads the board. "Are our prices competitive?" asks **Channel3**'s product search for comparable products and shows low / median / high market prices. "Refund the last order, it arrived cracked" issues a **Payments v2 refund** and notifies the customer when the signed `PAYMENT.CAPTURE.REFUNDED` arrives.
- **Safety nets.** Forged webhooks are rejected and change nothing. Duplicates are ignored. If a webhook is lost, a sweep re-reads the capture from PayPal. If the buyer closes the tab before returning, `CHECKOUT.ORDER.APPROVED` triggers the capture. If the webhook arrives before the capture response, the order stays PAID.

### How I built it
- **Backend:** Python + FastAPI and a SQLite ledger with an audit timeline.
- **PayPal:** Orders v2 and Payments v2 go through the official **PayPal Server SDK, which APIMatic generates** from PayPal's API definition. I integrated it with the skills of **APIMatic's PayPal Server SDK Context Plugin**, which ground a coding agent in the SDK's real surface (typed request models, the `ApiResponse` envelope, `ApiException`, and the `requests.Session` seam for testing). Webhook signature verification uses the REST API directly, because Notifications is not part of the generated SDK. A plain httpx client with the same interface is kept as a fallback (`PAYPAL_SDK=rest`).
- **AI:** a provider-agnostic tool-use loop. By default it runs **Claude (`claude-sonnet-5-5`)** through the official Anthropic SDK, with strict JSON-schema tools, low effort for chat latency and server-side refusal fallbacks. The same loop can run an offline "demo brain" that emits the same tool calls, so anyone can run the project with no keys.
- **Guardrails in code, not in the prompt:** the model never does arithmetic, it can't create a payment link in the same turn it priced the cart, there is a per-order cap, orders belong to their conversation, `PAID` is only set by a verified webhook or a direct PayPal read, and the copilot only *reports* market prices.
- **UI:** plain JavaScript. AG Grid Community 36 with a custom theme from the Theming API, custom cell renderers and filters driven by the copilot; AG Charts Community for the inventory chart; Bryntum Scheduler Community 8 (MIT) for the fulfillment board, with async server validation in `beforeEventDropFinalize` / `beforeEventResizeFinalize`.
- **Local PayPal sandbox simulator:** I wanted to build and test the full flow, including signed webhooks, before having sandbox credentials. So I wrote a small simulator that mirrors the documented endpoints, payloads and error names (`ORDER_NOT_APPROVED`, `ORDER_ALREADY_CAPTURED`…) and signs its webhooks. The SDK talks to it through a `requests` adapter; switching to the real sandbox is `PAYPAL_MODE=sandbox`.
- **Tests:** 36 automated tests cover pricing, reconciliation, the REST client and the Server SDK adapter (request shape and error mapping), signature failures, the full chat-to-verified-payment flow, forged/duplicate/lost/early webhooks, the closed-tab path, refunds, fulfillment planning and move validation, copilot grid views, inventory, and Channel3 parsing.
- **Demo video:** recorded by a script (Playwright headless browser + synthetic voice + ffmpeg), so the video shows the real app running end to end.

### Challenges I ran into
- **Making "paid" mean paid.** Return URLs, capture responses and webhooks arrive in any order. Moving the PayPal calls to the Server SDK made the signed webhook land before the capture response in my tests, which exposed a real race: the late capture response overwrote `PAID` with `CAPTURED`. Now an order the webhook settled never moves back, and there's a test for it.
- **Keeping the LLM away from money.** The model is great at conversation and bad at being an accountant. Pricing, confirmation, status and scheduling are tools with server-side checks.
- **Time zones on the board.** The workshop works on Buenos Aires time while the owner (or a judge) may be anywhere, so the board uses shop-local wall-clock times and shows only workshop hours.

### Accomplishments that I'm proud of
- An agent that actually transacts end to end (sell, charge, verify, reconcile, schedule, refund) with guardrails that hold regardless of which model is plugged in.
- Forged-webhook rejection you can trigger live from the console, and a board that refuses impossible moves.
- A repo a judge can run in five minutes with no accounts, with tests for every money path.

### What I learned
- The PayPal webhook signature verification API is the cleanest way to make agent-driven commerce trustworthy: the agent never has to "believe" anyone.
- A generated SDK plus a context plugin made the PayPal integration faster and more typed, and its different timing surfaced a bug that hand-written code had hidden.
- Tool design matters more than prompt design for agents that move money.

### What's next for Tendero
- WhatsApp Business and Instagram adapters (the agent is already channel-agnostic).
- Send courier tracking numbers to PayPal (Orders v2 trackers) when a pickup happens.
- Pay Later and Venmo as funding sources, and Payouts to split revenue between makers.
- PayPal Agent Toolkit / MCP server as an alternative tool backend for the owner copilot.

---

## Testing instructions (for judges)
1. `git clone <repo> && cd tendero && python -m venv .venv && .venv/Scripts/activate` (macOS/Linux: `source .venv/bin/activate`)
2. `pip install -r requirements.txt && python -m tendero.main` → open http://localhost:8000
3. In the customer chat type: `Hi! Do you ship to Madrid? I'd like 2 mate gourds and a bombilla`, then `Yes, send me the link`, then click **Pay with PayPal** → **Approve & pay**.
4. Watch the timeline (capture → webhook verified → reconciled → fulfillment planned → customer notified). Click **Send forged webhook** to see it rejected.
5. Open the **Fulfillment** tab (Bryntum Scheduler): drag the courier pickup to another hour and see the server refuse it; drag it to 17:00 on another weekday and it saves.
6. In *Owner copilot*: `Show me unpaid orders over $20` (filters the AG Grid ledger; start a second chat with **New chat** first if you want an unpaid order), `What do we ship next?`, `Are our prices competitive?` (Channel3; sample data unless `CHANNEL3_API_KEY` is set), `How did we do today? Any issues?` and `Refund the last order - the gourd arrived cracked`. Check the **Inventory** tab too.
7. Optional: set `ANTHROPIC_API_KEY` to run the agents on Claude; set `PAYPAL_MODE=sandbox` + sandbox credentials to use the real PayPal sandbox (README → "Use the real PayPal sandbox"); set `CHANNEL3_API_KEY` for live market prices.
8. `pip install -r requirements-dev.txt && pytest -q` → 36 passed.

No login needed. All data is fictional.

## AI tools used (Devpost field)
- Claude API (Anthropic), model `claude-sonnet-5-5`: runtime brain of the customer agent and the owner copilot (tool use).
- Claude Code (Anthropic): AI coding assistant used during development (code, tests, docs, video pipeline).
- APIMatic PayPal Server SDK Context Plugin: skills that guided the coding agent through the PayPal Server SDK integration.
- Microsoft Edge neural text-to-speech via the open-source `edge-tts` package: voice-over of the demo video.

## Sponsor tools used
- **AG Grid**: AG Grid Community (orders ledger, inventory, market prices) and AG Charts Community (inventory chart).
- **Bryntum**: Bryntum Scheduler Community (fulfillment board).
- **APIMatic**: PayPal Server SDK generated by APIMatic + APIMatic's PayPal Server SDK Context Plugin.
- **Channel3**: product search API for the owner's market price check.

## Prize categories this project fits

Rules note: a project can win at most one grand prize (or honorable mention) **and one** sponsor prize. Entering several sponsor categories only raises the chance of winning one of them.

- Grand prizes; **Best Use of Agentic Commerce**; **Best Use of PayPal + AI**; Most Impactful; Best Demo Delivery.
- **Best Use of AG Grid** (1st $5,000 / 2nd $2,000 / 3rd 3 × $1,000). Eligibility: "uses any AG Grid tools". AG Grid's page also rewards polish (custom widgets, theming, layout) and agent-driven use. Tendero: three themed grids (Theming API), custom cell renderers, filters and sorting set by the AI copilot from natural language (`show_orders` → `setFilterModel`/`applyColumnState`), inventory computed from verified PayPal payments with AG Charts. Not used: AG Studio and the Studio Agent Framework (commercial, trial needs a request form).
- **Best Use of Bryntum** (3 × $1,000). Eligibility: "uses any Bryntum tools". Tendero: Scheduler Community as the fulfillment board, fed by verified PayPal payments; dependencies, drag/resize with async server validation, tooltips, workshop-hours time axis.
- **Best Use of APIMatic** (3 × $1,000 + 6 months APIMatic business subscription). Eligibility: "uses any APIMatic tools". Tendero: every Orders v2 / Payments v2 call goes through the APIMatic-generated PayPal Server SDK, integrated with APIMatic's Context Plugin skills; SDK request shape and error mapping are tested.
- **Best Use of Channel3** ($1,500, 1 winner). Eligibility: "uses any Channel3 tools". Tendero: `/v1/search` powers the owner's market price check. The public demo uses a labelled sample response because there is no API key yet; the code calls the live API as soon as `CHANNEL3_API_KEY` is set.

## "Is this a new project?"
Yes, created from scratch during the submission period (first commit November 2026).
