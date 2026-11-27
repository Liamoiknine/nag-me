import re

import pytest

from nagme.app import normalize_phone
from tests.conftest import PHONE, turn_on, twilio_post

SETTINGS = {
    "phone_number": "(314) 555-0100",
    "coach_style": "sarcastic",
    "timezone": "America/New_York",
    "work_days": ["mon", "wed"],
    "start_time": "10:00",
    "end_time": "16:30",
    "morning_call": "1",
    "drift_enabled": "1",
    "drift_minutes": "20",
    "distractions": "YouTube.com\n\nreddit.com ",
    "max_retries": "1",
    "retry_delay_minutes": "10",
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(314) 555-0100", "+13145550100"),
        ("314.555.0100", "+13145550100"),
        ("1 314 555 0100", "+13145550100"),
        ("+44 20 7946 0958", "+442079460958"),
        ("555-0100", None),
        ("hello", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_dashboard_requires_sign_in(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.get("/scorecard", follow_redirects=False).status_code == 303


def test_wrong_password_is_rejected(client):
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_settings_round_trip(signed_in, manager):
    page = signed_in.post("/settings", data=SETTINGS).text
    assert "Settings saved." in page
    assert 'value="+13145550100"' in page
    assert re.search(r'value="sarcastic"\s+selected', page)
    s = manager.db.get_settings()
    assert s.distraction_list == ["youtube.com", "reddit.com"]
    assert s.morning_call and not s.review_call and s.drift_minutes == 20


def test_settings_validation(signed_in):
    bad = {**SETTINGS, "phone_number": "12", "timezone": "Mars/Base", "work_days": [],
           "start_time": "17:00", "end_time": "09:00", "drift_minutes": "2"}  # fmt: skip
    page = signed_in.post("/settings", data=bad).text
    for message in ("phone number", "time zone", "work day", "end after", "5 to 60"):
        assert message in page


def test_cannot_turn_on_without_a_phone_number(signed_in, manager):
    assert "Add your phone number" in signed_in.post("/toggle").text
    assert not manager.db.get_settings().active


def test_commitments_from_the_dashboard(signed_in, manager):
    turn_on(manager)
    page = signed_in.post("/commitments", data={"what": "Pitch deck", "minutes": "45"}).text
    assert "Added: Pitch deck, due 10:45 AM." in page
    signed_in.post("/commitments", data={"what": "Gym", "minutes": "18:00"})
    [deck, gym] = manager.db.open_commitments()
    page = signed_in.post(f"/commitments/{deck.id}", data={"status": "done"}).text
    assert manager.db.get_commitment(deck.id).status == "done"
    assert "Gym" in page
    page = signed_in.post("/plan", data={"plan": "Deck, then gym"}).text
    assert 'value="Deck, then gym"' in page
    scorecard = signed_in.get("/scorecard").text
    assert "Deck, then gym" in scorecard and "1/2" in scorecard


def test_snooze_from_the_dashboard(signed_in, manager):
    turn_on(manager)
    assert (
        "No automatic calls until 11:00 AM."
        in signed_in.post("/snooze", data={"minutes": "60"}).text
    )
    assert "Snooze cleared." in signed_in.post("/snooze", data={"minutes": "0"}).text


def test_twilio_webhooks_require_a_valid_signature(client):
    params = {"CallStatus": "completed"}
    assert twilio_post(client, "/twilio/status?call_id=1", params, sign=False).status_code == 403
    assert twilio_post(client, "/twilio/status?call_id=1", params).status_code == 204


def test_voice_webhook_connects_the_media_stream(signed_in, manager):
    turn_on(manager)
    signed_in.post("/call-now")
    call = manager.db.get_call(1)
    xml = twilio_post(signed_in, "/twilio/voice?call_id=1", {"CallSid": call.twilio_sid}).text
    assert '<Stream url="wss://nagme.example.test/twilio/stream">' in xml
    assert f'<Parameter name="token" value="{call.stream_token}"' in xml
    xml = twilio_post(signed_in, "/twilio/voice?call_id=99", {}).text
    assert "<Hangup" in xml


def test_calling_the_number_yourself(client, manager):
    turn_on(manager)
    xml = twilio_post(client, "/twilio/inbound", {"From": PHONE, "CallSid": "CA9"}).text
    assert "<Stream" in xml
    call = manager.db.get_call(1)
    assert call.purpose == "inbound" and call.status == "dialing" and call.twilio_sid == "CA9"
    stranger = twilio_post(client, "/twilio/inbound", {"From": "+12125550199", "CallSid": "CA8"})
    assert "<Hangup" in stranger.text and "<Stream" not in stranger.text
