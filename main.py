"""
WhatsApp AI Assistant — FastAPI backend.

Flow per incoming webhook call:
  1. Evolution API POSTs an inbound WhatsApp message to /webhook/whatsapp.
  2. We upsert the sender into `users` (Supabase) and log the inbound message
     into `chat_history`.
  3. We load the last 10 messages for that user as conversation context.
  4. We send that context to Gemini, with a `save_qualified_lead` tool.
  5. If Gemini decides the user is qualified (has stated a budget), it calls
     the tool, we write a row into `leads`, and feed the result back to
     Gemini so it can produce a natural-language reply.
  6. We log Gemini's reply into `chat_history` and send it back to the user
     via the Evolution API.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-ai-assistant")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # service_role key — bypasses RLS

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# "gemini-flash-latest" always resolves to Google's current default free-tier
# flash model, so it keeps working as older pinned versions get retired.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

EVOLUTION_API_URL = os.environ["EVOLUTION_API_URL"].rstrip("/")
EVOLUTION_API_KEY = os.environ["EVOLUTION_API_KEY"]
EVOLUTION_INSTANCE_NAME = os.environ["EVOLUTION_INSTANCE_NAME"]

CHAT_HISTORY_LIMIT = 10
MAX_TOOL_ITERATIONS = 4  # hard cap so a runaway tool loop can't hang a request

SYSTEM_PROMPT = """You are a WhatsApp sales assistant for a real estate company.
Speak naturally and briefly, as a human agent would over WhatsApp — no markdown,
no headers, short paragraphs.

Your job in this conversation:
1. Understand what the user is looking for.
2. Find out their budget for the purchase.
3. Once the user has clearly stated a specific numeric budget, call the
   save_qualified_lead tool with that amount. Do this only once per
   conversation, the first time a budget is stated.
4. After qualifying the lead, let the user know a specialist will follow up,
   and keep answering their questions normally.

Never invent a budget the user did not state. If the user gives a range,
use the lower bound.
"""

SAVE_QUALIFIED_LEAD_FUNCTION = types.FunctionDeclaration(
    name="save_qualified_lead",
    description=(
        "Call this exactly once, as soon as the user has stated a clear "
        "numeric budget for their purchase and appears to be a real "
        "prospect. This persists them as a qualified lead in the CRM."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "budget": {
                "type": "number",
                "description": "The budget the user stated, as a plain number (no currency symbols).",
            }
        },
        "required": ["budget"],
    },
)

GENERATE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=[SAVE_QUALIFIED_LEAD_FUNCTION])],
)

# -----------------------------------------------------------------------------
# Clients
# -----------------------------------------------------------------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(title="WhatsApp AI Assistant")


# -----------------------------------------------------------------------------
# Supabase helpers
# -----------------------------------------------------------------------------
def get_or_create_user(phone: str, name: str | None) -> dict[str, Any]:
    """Find a user by phone, creating one if this is their first message."""
    existing = (
        supabase.table("users").select("*").eq("phone", phone).limit(1).execute()
    )
    if existing.data:
        return existing.data[0]

    created = (
        supabase.table("users")
        .insert({"phone": phone, "name": name})
        .execute()
    )
    return created.data[0]


def save_chat_message(user_id: str, role: str, text: str) -> None:
    supabase.table("chat_history").insert(
        {"user_id": user_id, "role": role, "message_text": text}
    ).execute()


def get_recent_history(user_id: str, limit: int = CHAT_HISTORY_LIMIT) -> list[dict[str, str]]:
    """Return the last `limit` messages for this user, oldest first, as
    {role, message_text} rows (role is 'user' or 'assistant', matching the
    chat_history table — mapped to Gemini's 'user'/'model' roles later)."""
    result = (
        supabase.table("chat_history")
        .select("role, message_text, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = list(reversed(result.data))
    return [{"role": row["role"], "content": row["message_text"]} for row in rows]


def save_qualified_lead(user_id: str, budget: float) -> dict[str, Any]:
    """Tool implementation: persist a qualified lead for this user."""
    created = (
        supabase.table("leads")
        .insert(
            {
                "user_id": user_id,
                "budget": budget,
                "status": "qualified",
                "crm_synced": False,
            }
        )
        .execute()
    )
    logger.info("Saved qualified lead for user %s with budget %s", user_id, budget)
    return created.data[0]


# -----------------------------------------------------------------------------
# Evolution API (WhatsApp gateway) helpers
# -----------------------------------------------------------------------------
async def send_whatsapp_message(phone: str, text: str) -> None:
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": phone, "text": text}

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.is_error:
            logger.error(
                "Evolution API send failed (%s): %s", response.status_code, response.text
            )
            response.raise_for_status()


def extract_incoming_message(payload: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """Pull (phone, text, push_name) out of an Evolution API webhook payload.

    Returns None if this payload isn't an inbound user text message we should
    respond to (e.g. it's our own outbound echo, a status update, or media
    without a caption).
    """
    if payload.get("event") != "messages.upsert":
        return None

    data = payload.get("data") or {}
    key = data.get("key") or {}

    if key.get("fromMe"):
        return None  # ignore our own outgoing messages

    remote_jid = key.get("remoteJid", "")
    phone = re.sub(r"@.*$", "", remote_jid)
    if not phone:
        return None

    message = data.get("message") or {}
    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
    )
    if not text:
        return None  # media/sticker/reaction with no plain text — skip

    push_name = data.get("pushName")
    return phone, text, push_name


# -----------------------------------------------------------------------------
# Gemini conversation loop
# -----------------------------------------------------------------------------
def _to_gemini_contents(history: list[dict[str, str]]) -> list[types.Content]:
    """Map chat_history rows ({'role': 'user'|'assistant', 'content': str})
    to Gemini Content objects. Gemini uses 'model' where our schema (and the
    Anthropic convention it was originally written for) uses 'assistant'."""
    contents: list[types.Content] = []
    for row in history:
        gemini_role = "model" if row["role"] == "assistant" else "user"
        contents.append(
            types.Content(role=gemini_role, parts=[types.Part.from_text(text=row["content"])])
        )
    return contents


def run_gemini_turn(history: list[dict[str, str]], user_id: str) -> str:
    """Send the conversation to Gemini, executing any function calls it
    makes, and return the final assistant text reply."""
    contents = _to_gemini_contents(history)

    for _ in range(MAX_TOOL_ITERATIONS):
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=GENERATE_CONFIG,
        )

        function_calls = response.function_calls or []
        if not function_calls:
            return response.text or ""

        # Echo the model's turn (including function_call parts) back into history.
        contents.append(response.candidates[0].content)

        tool_response_parts: list[types.Part] = []
        for call in function_calls:
            if call.name == "save_qualified_lead":
                budget = (call.args or {}).get("budget")
                try:
                    save_qualified_lead(user_id, budget)
                    result: dict[str, Any] = {"status": "success", "budget": budget}
                except Exception as exc:  # noqa: BLE001 — surface any DB error to Gemini
                    logger.exception("Failed to save qualified lead")
                    result = {"status": "error", "message": str(exc)}
            else:
                result = {"status": "error", "message": f"unknown tool: {call.name}"}

            tool_response_parts.append(
                types.Part.from_function_response(name=call.name, response=result)
            )

        contents.append(types.Content(role="tool", parts=tool_response_parts))

    logger.warning("Hit MAX_TOOL_ITERATIONS for user %s without a final reply", user_id)
    return "Sorry, I'm having trouble processing that right now — a specialist will follow up shortly."


# -----------------------------------------------------------------------------
# Webhook endpoint
# -----------------------------------------------------------------------------
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request) -> JSONResponse:
    payload = await request.json()

    extracted = extract_incoming_message(payload)
    if extracted is None:
        return JSONResponse({"status": "ignored"}, status_code=status.HTTP_200_OK)

    phone, incoming_text, push_name = extracted

    user = get_or_create_user(phone, push_name)
    user_id = user["id"]

    save_chat_message(user_id, "user", incoming_text)

    history = get_recent_history(user_id, limit=CHAT_HISTORY_LIMIT)

    reply_text = run_gemini_turn(history, user_id)

    save_chat_message(user_id, "assistant", reply_text)
    await send_whatsapp_message(phone, reply_text)

    return JSONResponse({"status": "ok"}, status_code=status.HTTP_200_OK)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
