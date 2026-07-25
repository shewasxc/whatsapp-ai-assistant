# WhatsApp AI Assistant

A FastAPI backend that turns WhatsApp into an automated lead-qualification
channel for a real-estate sales team. An LLM (Google Gemini) carries the
conversation in natural language and decides, on its own, when a visitor is
a real prospect — at which point it calls a tool that persists them as a
qualified lead, ready for a human agent to follow up on.

## The problem this solves

Sales teams that get inbound leads over WhatsApp lose time triaging every
conversation by hand: "is this person serious? what's their budget? do they
need a callback?" This bot has that first conversation automatically —
24/7, in natural language, no forms — and only surfaces a contact once
they've stated a real budget. A human agent only sees qualified leads, not
raw chat noise.

## What's implemented

- **Inbound WhatsApp → LLM conversation**, with full chat history persisted per user
- **Automatic lead qualification** via LLM tool-calling — the model decides when to save a lead, not a keyword regex
- **Idempotent webhook handling** — a redelivered WhatsApp event never causes a duplicate reply or a duplicate lead
- **Graceful failure handling** — an internal error (LLM outage, DB hiccup) never bounces back to the WhatsApp gateway as a crash; the user gets a polite fallback message instead
- **A `GET /leads` endpoint** to pull qualified leads out of the system without touching the database directly
- **A test suite** covering the webhook contract, the tool-calling loop, and the failure paths — not just the happy path

## How it works

```
WhatsApp user
    │  sends a message
    ▼
Evolution API (self-hosted WhatsApp gateway, Baileys-based)
    │  POST /webhook/whatsapp   { event: "messages.upsert", data: {...} }
    ▼
FastAPI backend (this repo)
    │  1. skip if this wa_message_id was already processed (idempotency)
    │  2. find-or-create the user in Supabase, by phone number
    │  3. log the inbound message to chat_history
    │  4. load the last 10 messages as conversation context
    │  5. send that context to Gemini, with a save_qualified_lead tool
    │  6. if Gemini calls the tool → insert a row into leads
    │  7. log Gemini's reply to chat_history
    ▼
Evolution API  →  reply sent back to the WhatsApp user
```

Everything is stored in Supabase (Postgres): `users`, `chat_history`,
`leads`. See [`schema.sql`](schema.sql) for the full schema, indexes, and RLS
policies.

## Why these tools

| Choice | Reason |
|---|---|
| **Gemini** (`gemini-flash-latest`) over Claude/GPT | Free tier with no billing required. The `-latest` alias tracks whichever flash model Google currently keeps default, so it survives model retirements (a pinned version like `gemini-2.5-flash` *will* eventually 404 for new API keys — this bit us once already in production). |
| **Evolution API** (self-hosted, Baileys) over the official WhatsApp Cloud API | Free, no Meta business verification needed. Trade-off: it's an unofficial client, so the connected number is at real risk of being rate-limited or banned by WhatsApp (see **Risks** below) — the official Cloud API is the only way to eliminate that risk, at the cost of paying for it and going through business verification. |
| **Supabase** over a self-managed Postgres | Free managed Postgres with RLS built in, service-role key bypasses RLS for the backend while leaving a safe default-deny posture for any other key. |
| **Render (free tier)** for hosting | No credit card required. Trade-off: the free tier spins down after ~15 minutes of inactivity, so the first request after a quiet period takes 30–50s to wake up. |

## Setup

### 1. Database

Run [`schema.sql`](schema.sql) in the Supabase SQL Editor of a fresh project.
It's written to be safely re-run (`create if not exists` / `add column if
not exists`), so re-applying it after a pull is always safe.

### 2. Environment variables

Copy `.env.example` to `.env` and fill in real values:

| Variable | Where to get it |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase → Project Settings → API (`SUPABASE_KEY` is the **service_role** key, not `anon`) |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GEMINI_MODEL` | Optional, defaults to `gemini-flash-latest` |
| `EVOLUTION_API_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE_NAME` | Your Evolution API deployment and the instance you create on it |
| `ADMIN_API_KEY` | Optional — any random string. Required only to use `GET /leads`; the endpoint stays disabled if unset. |

### 3. Evolution API (WhatsApp gateway)

Deploy `evoapicloud/evolution-api:latest` (see [`docker-compose.yml`](docker-compose.yml)
for local reference config), then:

```bash
# create an instance and get a pairing code (no QR scan needed)
curl -X POST "$EVOLUTION_API_URL/instance/create" \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d '{"instanceName":"'"$EVOLUTION_INSTANCE_NAME"'","integration":"WHATSAPP-BAILEYS","qrcode":true,"number":"<phone>"}'

# point it at this backend
curl -X POST "$EVOLUTION_API_URL/webhook/set/$EVOLUTION_INSTANCE_NAME" \
  -H "apikey: $EVOLUTION_API_KEY" -H "Content-Type: application/json" \
  -d '{"webhook":{"enabled":true,"url":"<this-backend-url>/webhook/whatsapp","events":["MESSAGES_UPSERT"]}}'
```

### 4. Run the backend

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## API surface

| Endpoint | Purpose |
|---|---|
| `POST /webhook/whatsapp` | Evolution API calls this on every inbound event. Always returns `200` (`ok` / `ignored` / `duplicate` / `error`) — see **Production-readiness notes** below for why. |
| `GET /leads?status=qualified` | Lists leads, newest first, joined with the owning user's phone/name. Requires header `x-admin-key: <ADMIN_API_KEY>`; `status` filter is optional. |
| `GET /health` | Liveness check for uptime monitors / Render health checks. |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

18 tests covering: webhook payload parsing, the Gemini tool-calling loop
(including the `save_qualified_lead` path and the max-iterations fallback),
idempotent redelivery handling, the internal-error fallback path, the
`/leads` auth gate, and `/webhook/whatsapp` end to end — all with
Supabase/Gemini/Evolution API mocked out, so the suite runs with no network
access and no real credentials.

## Production-readiness notes

- **Idempotency**: WhatsApp gateways can redeliver the same `messages.upsert`
  event (e.g. after a reconnect). Every inbound message's WhatsApp message ID
  is checked against `chat_history` before any processing happens, so a
  redelivery is a no-op instead of a duplicate reply or duplicate lead.
- **Failure isolation**: the webhook handler wraps the whole processing
  pipeline in a `try/except`. A failure (LLM timeout, DB error, anything) is
  logged with full context and answered with a graceful message to the user
  — it never surfaces as an HTTP 500 to the gateway, which would otherwise
  just retry the same failing request indefinitely.
- **Delivery retries**: outbound replies to Evolution API get one retry on
  transient HTTP failure before giving up and logging.

## Known limitations

- Text messages only — no audio, images, or documents.
- Single tool (`save_qualified_lead`); no CRM push (the `leads.crm_synced`
  column exists but nothing sets it yet — the natural next integration).
- `/leads` is a plain JSON endpoint gated by a static API key, not a UI or a
  proper auth system — fine for internal/API use, not for handing to a
  non-technical sales team as-is.

## Risk: unofficial WhatsApp client

Evolution API talks to WhatsApp over the same protocol as WhatsApp Web,
without Meta's sanction. WhatsApp does detect and restrict numbers that look
automated — frequent reconnects, datacenter IPs, and sudden bot-like message
patterns are all signals that increase that risk. Mitigations:

- Use a number dedicated to the bot, not a personal daily-driver number.
- Keep the gateway's connection stable — avoid repeated
  disconnect/reconnect cycles.
- Ramp up message volume gradually rather than starting with a burst.

The official [WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api)
is the only way to remove this risk entirely, at the cost of Meta business
verification and per-conversation pricing. For a paying client this would be
the recommended production path — this repo defaults to the free
self-hosted gateway to keep the whole stack free to run.
