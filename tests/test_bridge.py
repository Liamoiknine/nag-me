"""A whole simulated call: Twilio webhooks -> media stream <-> fake OpenAI, with tools."""

import asyncio
import time

from nagme.schedule import local_date
from tests.conftest import turn_on, twilio_post


def start_call(client, manager):
    turn_on(manager, morning_call=False, review_call=False)
    call = asyncio.run(manager.call_now())
    xml = twilio_post(client, f"/twilio/voice?call_id={call.id}", {"CallSid": call.twilio_sid}).text
    assert call.stream_token in xml
    return call


def start_message(call, token=None):
    return {
        "event": "start",
        "start": {
            "streamSid": "MZ1",
            "callSid": call.twilio_sid,
            "customParameters": {"call_id": str(call.id), "token": token or call.stream_token},
        },
    }


def media(ts: int) -> dict:
    return {"event": "media", "media": {"timestamp": str(ts), "payload": "/////w=="}}


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_full_call_records_a_commitment(client, manager, telephony, realtime):
    call = start_call(client, manager)

    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json(start_message(call))

        greeting = ws.receive_json()
        assert greeting["event"] == "media" and greeting["media"]["payload"] == "AAAA"
        mark = ws.receive_json()
        ws.send_json({"event": "mark", "streamSid": "MZ1", "mark": mark["mark"]})

        for ts in (100, 120, 140):
            ws.send_json(media(ts))

        goodbye = ws.receive_json()
        assert goodbye["media"]["payload"] == "BBBB"
        bye_mark = ws.receive_json()
        assert not telephony.hung_up  # waits for the sign-off to finish playing
        ws.send_json({"event": "mark", "streamSid": "MZ1", "mark": bye_mark["mark"]})
        assert wait_for(lambda: telephony.hung_up == [call.twilio_sid])
        ws.send_json({"event": "stop"})

    assert wait_for(lambda: manager.db.get_call(call.id).status == "completed")
    saved = manager.db.get_call(call.id)
    assert saved.summary == "Finishing the quarterly report by 10:30."
    assert saved.on_track == "yes"
    today = local_date(manager.clock(), manager.db.get_settings())
    assert manager.db.get_plan(today) == "Ship the quarterly report."
    assert saved.transcript == [
        {"role": "coach", "text": "Morning. What's the plan today?"},
        {"role": "user", "text": "I'll finish the quarterly report in 30 minutes."},
        {"role": "coach", "text": "Report by 10:30. I'll call then."},
    ]
    [commitment] = manager.db.open_commitments()
    assert commitment.text == "Quarterly report"
    assert manager.local_time(commitment.due_at) == "10:30 AM"

    session = realtime.sent[0]["session"]
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "low",
    }
    assert [t["name"] for t in session["tools"]] == [
        "add_commitment",
        "update_commitment",
        "finish_call",
    ]
    outputs = [e["item"] for e in realtime.sent if e["type"] == "conversation.item.create"]
    assert outputs[0]["call_id"] == "fc_1" and '"due": "10:30 AM"' in outputs[0]["output"]
    # Exactly one follow-up response after the tool result, and none after finish_call.
    assert [e for e in realtime.sent if e == {"type": "response.create"}] == [
        {"type": "response.create"}
    ]


def test_interrupting_the_coach_cuts_its_audio(client, manager, realtime):
    realtime.barge_in = True
    call = start_call(client, manager)
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json(start_message(call))
        assert ws.receive_json()["event"] == "media"
        assert ws.receive_json()["event"] == "mark"
        assert ws.receive_json() == {"event": "clear", "streamSid": "MZ1"}
        ws.send_json({"event": "stop"})
    truncate = next(e for e in realtime.sent if e["type"] == "conversation.item.truncate")
    assert truncate["item_id"] == "item_greet" and truncate["content_index"] == 0


def test_stream_with_a_bad_token_is_refused(client, manager, realtime):
    call = start_call(client, manager)
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json(start_message(call, token="forged"))
        message = ws.receive()
        assert message["type"] == "websocket.close" and message["code"] == 1008
    assert realtime.sent == []
    assert manager.db.get_call(call.id).status == "dialing"


def test_bad_tool_call_is_answered_not_fatal(client, manager, realtime):
    realtime.scripted_reply = False
    call = start_call(client, manager)
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json(start_message(call))
        ws.receive_json()
        ws.receive_json()
        realtime.tool_call("fc_x", "add_commitment", {"what": "Memo", "due_time": "soon"})
        realtime.tool_call("fc_y", "finish_call", {"summary": "half"}, status="incomplete")
        realtime.emit(type="response.done")
        assert wait_for(lambda: any(e["type"] == "conversation.item.create" for e in realtime.sent))
        ws.send_json({"event": "stop"})
    output = next(e for e in realtime.sent if e["type"] == "conversation.item.create")
    assert "14:30" in output["item"]["output"]  # the error explains the format
    assert manager.db.get_call(call.id).summary is None  # incomplete finish_call ignored
    assert manager.db.open_commitments() == []


def test_call_without_finish_call_is_summarized_afterwards(client, manager, realtime):
    realtime.scripted_reply = False  # the coach never calls finish_call
    call = start_call(client, manager)
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json(start_message(call))
        ws.receive_json()
        ws.receive_json()
        for ts in (100, 120, 140):
            ws.send_json(media(ts))
        assert wait_for(lambda: any(e.get("type") == "response.create" and "response" not in e
                                    for e in realtime.sent))  # fmt: skip
        ws.send_json({"event": "stop"})  # you hung up
    assert wait_for(lambda: manager.db.get_call(call.id).summary == "Summarized afterwards.")
    today = local_date(manager.clock(), manager.db.get_settings())
    assert manager.db.get_plan(today) == "Fallback plan"


def test_prompt_handles_hello_and_paused_callbacks(manager):
    from nagme.coach import build_instructions

    turn_on(manager)
    call = manager.db.create_call("manual", manager.clock())
    text = build_instructions(manager.call_context(call))
    assert "If they just say hello" in text and "finish_call in that same turn" in text
