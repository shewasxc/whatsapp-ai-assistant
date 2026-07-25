from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


def _upsert_payload(text="hola", from_me=False, extended=False):
    message = {"extendedTextMessage": {"text": text}} if extended else {"conversation": text}
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"remoteJid": "34600000000@s.whatsapp.net", "fromMe": from_me},
            "message": message,
            "pushName": "Test User",
        },
    }


# -----------------------------------------------------------------------------
# extract_incoming_message
# -----------------------------------------------------------------------------
def test_extract_incoming_message_conversation_text():
    result = main.extract_incoming_message(_upsert_payload("hola"))
    assert result == ("34600000000", "hola", "Test User")


def test_extract_incoming_message_extended_text():
    result = main.extract_incoming_message(_upsert_payload("hola", extended=True))
    assert result == ("34600000000", "hola", "Test User")


def test_extract_incoming_message_ignores_non_upsert_event():
    payload = {"event": "connection.update", "data": {}}
    assert main.extract_incoming_message(payload) is None


def test_extract_incoming_message_ignores_own_outgoing_message():
    assert main.extract_incoming_message(_upsert_payload(from_me=True)) is None


def test_extract_incoming_message_returns_none_without_text():
    payload = _upsert_payload()
    payload["data"]["message"] = {"stickerMessage": {}}
    assert main.extract_incoming_message(payload) is None


# -----------------------------------------------------------------------------
# _to_gemini_contents
# -----------------------------------------------------------------------------
def test_to_gemini_contents_maps_assistant_role_to_model():
    history = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "hola! en que puedo ayudarte?"},
    ]
    contents = main._to_gemini_contents(history)

    assert [c.role for c in contents] == ["user", "model"]
    assert contents[0].parts[0].text == "hola"
    assert contents[1].parts[0].text == "hola! en que puedo ayudarte?"


# -----------------------------------------------------------------------------
# run_gemini_turn — the manual function-calling loop
# -----------------------------------------------------------------------------
def test_run_gemini_turn_returns_text_when_no_function_call():
    response = MagicMock(function_calls=[], text="hola! como puedo ayudarte?")

    with patch("main.gemini_client") as mock_client:
        mock_client.models.generate_content.return_value = response
        reply = main.run_gemini_turn(
            history=[{"role": "user", "content": "hola"}], user_id="user-123"
        )

    assert reply == "hola! como puedo ayudarte?"
    mock_client.models.generate_content.assert_called_once()


def test_run_gemini_turn_saves_lead_on_tool_call():
    tool_call_response = MagicMock(
        function_calls=[SimpleNamespace(name="save_qualified_lead", args={"budget": 150000})],
        candidates=[MagicMock(content="model-turn-with-call")],
    )
    final_response = MagicMock(
        function_calls=[], text="Gracias, un especialista te contactara pronto."
    )

    with patch("main.gemini_client") as mock_client, patch(
        "main.save_qualified_lead"
    ) as mock_save_lead:
        mock_client.models.generate_content.side_effect = [tool_call_response, final_response]
        mock_save_lead.return_value = {"id": "lead-1", "budget": 150000}

        reply = main.run_gemini_turn(
            history=[{"role": "user", "content": "mi presupuesto es 150000"}],
            user_id="user-123",
        )

    assert reply == "Gracias, un especialista te contactara pronto."
    mock_save_lead.assert_called_once_with("user-123", 150000)
    assert mock_client.models.generate_content.call_count == 2


def test_run_gemini_turn_stops_after_max_iterations():
    looping_response = MagicMock(
        function_calls=[SimpleNamespace(name="save_qualified_lead", args={"budget": 1})],
        candidates=[MagicMock(content="model-turn")],
    )

    with patch("main.gemini_client") as mock_client, patch("main.save_qualified_lead"):
        mock_client.models.generate_content.return_value = looping_response
        reply = main.run_gemini_turn(history=[], user_id="user-123")

    assert "specialist will follow up" in reply
    assert mock_client.models.generate_content.call_count == main.MAX_TOOL_ITERATIONS


# -----------------------------------------------------------------------------
# POST /webhook/whatsapp
# -----------------------------------------------------------------------------
def test_webhook_ignores_non_upsert_event():
    response = client.post("/webhook/whatsapp", json={"event": "connection.update", "data": {}})
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_webhook_ignores_own_outgoing_message():
    response = client.post("/webhook/whatsapp", json=_upsert_payload(from_me=True))
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


@patch("main.send_whatsapp_message", new_callable=AsyncMock)
@patch("main.run_gemini_turn")
@patch("main.get_recent_history")
@patch("main.save_chat_message")
@patch("main.get_or_create_user")
def test_webhook_happy_path(
    mock_get_or_create_user,
    mock_save_chat_message,
    mock_get_recent_history,
    mock_run_gemini_turn,
    mock_send_whatsapp_message,
):
    mock_get_or_create_user.return_value = {"id": "user-123", "phone": "34600000000"}
    mock_get_recent_history.return_value = [{"role": "user", "content": "hola"}]
    mock_run_gemini_turn.return_value = "hola! como puedo ayudarte?"

    response = client.post("/webhook/whatsapp", json=_upsert_payload("hola"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_get_or_create_user.assert_called_once_with("34600000000", "Test User")
    mock_run_gemini_turn.assert_called_once_with(
        mock_get_recent_history.return_value, "user-123"
    )
    mock_send_whatsapp_message.assert_awaited_once_with(
        "34600000000", "hola! como puedo ayudarte?"
    )
    assert mock_save_chat_message.call_count == 2
    mock_save_chat_message.assert_any_call("user-123", "user", "hola")
    mock_save_chat_message.assert_any_call("user-123", "assistant", "hola! como puedo ayudarte?")


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
