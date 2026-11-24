import re

import pytest

from nagme.app import normalize_phone
from tests.conftest import twilio_post


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


def test_wrong_password_is_rejected(client):
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_settings_round_trip(signed_in):
    response = signed_in.post(
        "/settings",
        data={
            "phone_number": "(314) 555-0100",
            "coach_style": "sarcastic",
            "timezone": "America/New_York",
            "work_days": ["mon", "wed"],
            "start_time": "10:00",
            "end_time": "16:30",
            "interval_minutes": "45",
            "max_retries": "1",
            "retry_delay_minutes": "10",
        },
    )
    page = response.text
    assert "Settings saved." in page
    assert 'value="+13145550100"' in page
    assert re.search(r'value="sarcastic"\s+selected', page)
    assert 'value="16:30"' in page


def test_settings_validation(signed_in):
    page = signed_in.post(
        "/settings",
        data={
            "phone_number": "12",
            "coach_style": "supportive",
            "timezone": "Mars/Base",
            "start_time": "17:00",
            "end_time": "09:00",
            "interval_minutes": "1",
            "max_retries": "2",
            "retry_delay_minutes": "5",
        },
    ).text
    for message in ("phone number", "time zone", "work day", "end after", "10 to 240"):
        assert message in page


def test_cannot_turn_on_without_a_phone_number(signed_in, manager):
    assert "Add your phone number" in signed_in.post("/toggle").text
    assert not manager.db.get_settings().active


def test_twilio_webhooks_require_a_valid_signature(client):
    assert (
        twilio_post(
            client, "/twilio/status?call_id=1", {"CallStatus": "completed"}, sign=False
        ).status_code
        == 403
    )
    assert (
        twilio_post(client, "/twilio/status?call_id=1", {"CallStatus": "completed"}).status_code
        == 204
    )


def test_voice_webhook_connects_the_media_stream(signed_in, manager):
    signed_in.post(
        "/settings",
        data={
            "phone_number": "3145550100",
            "coach_style": "strict",
            "timezone": "America/Chicago",
            "work_days": ["mon", "tue", "wed", "thu", "fri"],
            "start_time": "09:00",
            "end_time": "17:00",
            "interval_minutes": "60",
            "max_retries": "2",
            "retry_delay_minutes": "5",
        },
    )
    signed_in.post("/call-now")
    call = manager.db.get_call(1)
    xml = twilio_post(signed_in, "/twilio/voice?call_id=1", {"CallSid": call.twilio_sid}).text
    assert '<Stream url="wss://nagme.example.test/twilio/stream">' in xml
    assert f'<Parameter name="token" value="{call.stream_token}"' in xml

    # A stale or unknown call gets a polite hang-up instead.
    xml = twilio_post(signed_in, "/twilio/voice?call_id=99", {}).text
    assert "<Hangup" in xml
