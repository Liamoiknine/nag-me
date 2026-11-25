"""A whole simulated call: scheduler -> Twilio webhooks -> media stream <-> fake OpenAI."""

import asyncio
import time

from tests.conftest import twilio_post
from tests.test_calls import turn_on


def start_call(client, manager):
    turn_on(manager)
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


def test_full_check_in_call(client, manager, telephony, realtime):
    call = start_call(client, manager)

    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json(start_message(call))

        greeting = ws.receive_json()
        assert greeting["event"] == "media" and greeting["media"]["payload"] == "AAAA"
        mark = ws.receive_json()
        assert mark["event"] == "mark"
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
    twilio_post(client, f"/twilio/status?call_id={call.id}", {"CallStatus": "completed"})

    saved = manager.db.get_call(call.id)
    assert saved.status == "completed"
    assert saved.summary == "Working on the quarterly report."
    assert saved.on_track == "yes"
    assert saved.plan == "Finish the quarterly report by 3pm."
    assert saved.transcript == [
        {"role": "coach", "text": "Morning. What's the plan today?"},
        {"role": "user", "text": "Finishing the quarterly report."},
        {"role": "coach", "text": "Good. Go get it done."},
    ]

    session = realtime.sent[0]
    assert session["type"] == "session.update"
    audio_in = session["session"]["audio"]["input"]
    assert audio_in["format"] == {"type": "audio/pcmu"}
    assert audio_in["turn_detection"] == {"type": "semantic_vad", "eagerness": "low"}
    assert audio_in["transcription"]["language"] == "en"
    assert "first check-in today" in session["session"]["instructions"]
    assert realtime.sent[1]["type"] == "response.create"
    assert sum(e["type"] == "input_audio_buffer.append" for e in realtime.sent) == 3
    output = next(e for e in realtime.sent if e["type"] == "conversation.item.create")
    assert output["item"] == {
        "type": "function_call_output",
        "call_id": "fc_1",
        "output": '{"ok": true}',
    }

    page = client.post("/login", data={"password": "hunter2"}).text
    assert "Working on the quarterly report." in page
    assert "Finishing the quarterly report." in page


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
