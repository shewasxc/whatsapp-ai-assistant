# WhatsApp AI Assistant

A FastAPI backend that turns WhatsApp into a lead-qualification channel for a
real-estate sales team. An LLM (Google Gemini) carries the conversation,
decides when a visitor is a real prospect, and calls a tool to persist them
as a qualified lead — no manual triage needed.

## How it works

```
WhatsApp user
    │  sends a message
    ▼
Evolution API (self-hosted WhatsApp gateway, Baileys-based)
    │  POST /webhook/whatsapp   { event: "messages.upsert", data: {...} }
    ▼
FastAPI backend (this repo)
    │  1. find-or-create the user in Supabase, by phone number
    │  2. log the inbound message to chat_history
    │  3. load the last 10 messages as conversation context
    │  4. send that context to Gemini, with a save_qualified_lead tool
    │  5. if Gemini calls the tool → insert a row into leads
    │  6. log Gemini's reply to chat_history
    ▼
Evolution API  →  reply sent back to the WhatsApp user
```

Everything is stored in Supabase (Postgres): `users`, `chat_history`,
`leads`. See [`schema.sql`](schema.sql) for the full schema, indexes, and RLS
policies.

## Why these tools

| Choice | Reason |
|---|---|
| **Gemini** (`gemini-flash-latest`) over Claude/GPT | Free tier with no billing required. The `-latest` alias tracks whichever flash model Google currently keeps default, so it survives model retirements (a pinned version like `gemini-2.5-flash` *will* eventually 404 for new API keys — this bit us once already). |
| **Evolution API** (self-hosted, Baileys) over the official WhatsApp Cloud API | Free, no Meta business verification needed. Trade-off: it's an unofficial client, so the connected number is at real risk of being rate-limited or banned by WhatsApp (see **Risks** below) — the official Cloud API is the only way to eliminate that risk, at the cost of paying for it and going through business verification. |
| **Supabase** over a self-managed Postgres | Free managed Postgres with RLS built in, service-role key bypasses RLS for the backend while leaving a safe default-deny posture for any other key. |
| **Render (free tier)** for hosting | No credit card required. Trade-off: the free tier spins down after ~15 minutes of inactivity, so the first request after a quiet period takes 30–50s to wake up. |

## Setup

### 1. Database

Run [`schema.sql`](schema.sql) in the Supabase SQL Editor of a fresh project.

### 2. Environment variables

Copy `.env.example` to `.env` and fill in real values:

| Variable | Where to get it |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase → Project Settings → API (`SUPABASE_KEY` is the **service_role** key, not `anon`) |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GEMINI_MODEL` | Optional, defaults to `gemini-flash-latest` |
| `EVOLUTION_API_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE_NAME` | Your Evolution API deployment and the instance you create on it |

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

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers webhook payload parsing, the Gemini tool-calling loop (including the
`save_qualified_lead` path and the max-iterations fallback), and the
`/webhook/whatsapp` endpoint end to end with Supabase/Gemini/Evolution API
mocked out.

## Known limitations

- Text messages only — no audio, images, or documents.
- Single tool (`save_qualified_lead`); no CRM push (the `leads.crm_synced`
  column exists but nothing sets it yet).
- No way to view leads besides the Supabase Table Editor / SQL.
- No retry/backoff around Gemini or Evolution API calls — a transient
  failure surfaces as a 500 to the webhook caller.

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
verification and per-conversation pricing.
