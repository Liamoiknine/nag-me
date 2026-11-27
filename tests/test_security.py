"""Each test tries an attack and checks it fails."""

from datetime import timedelta

import pytest

from nagme.security import LoginLimiter
from tests.conftest import PHONE, csrf_from, login, turn_on, twilio_post

TUNNEL = {"cf-connecting-ip": "203.0.113.9", "cf-ray": "abc123"}


# Reaching the dashboard through the public tunnel


@pytest.mark.parametrize("path", ["/", "/login", "/scorecard", "/static/style.css"])
def test_dashboard_is_invisible_through_the_tunnel(client, path):
    response = client.get(path, headers=TUNNEL, follow_redirects=False)
    assert response.status_code == 404


def test_cannot_post_to_the_dashboard_through_the_tunnel(signed_in, manager):
    turn_on(manager)
    response = signed_in.post("/toggle", headers={"x-forwarded-for": "203.0.113.9"})
    assert response.status_code == 404
    assert manager.db.get_settings().active  # unchanged


def test_twilio_routes_still_work_through_the_tunnel(client):
    response = twilio_post(client, "/twilio/status?call_id=1", {"CallStatus": "completed"})
    assert response.status_code == 204
    assert client.get("/healthz", headers=TUNNEL).text == "ok"


def test_remote_dashboard_can_be_opted_into(cfg, telephony):
    import dataclasses

    from fastapi.testclient import TestClient

    from nagme.app import create_app

    app = create_app(
        dataclasses.replace(cfg, allow_remote_dashboard=True),
        telephony=telephony,
        run_background=False,
    )
    with TestClient(app) as c:
        assert c.get("/login", headers=TUNNEL).status_code == 200


# Browser protections


def test_security_headers(client):
    headers = client.get("/login").headers
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "script-src" not in headers["content-security-policy"]  # default-src 'none'
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"


def test_forged_form_without_token_is_rejected(signed_in, manager):
    turn_on(manager)
    response = signed_in.request("POST", "/toggle", data={})  # no token, like a forged form
    assert response.status_code == 403
    assert manager.db.get_settings().active


def test_cross_site_origin_is_rejected_even_with_a_token(signed_in, manager):
    turn_on(manager)
    response = signed_in.post("/toggle", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert manager.db.get_settings().active


def test_login_needs_a_token_too(client):
    response = client.post("/login", data={"password": "hunter2"})
    assert response.status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 303


def test_session_cookie_is_httponly_and_samesite(client):
    cookie = client.get("/login").headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


# Password guessing


def test_login_locks_out_after_five_wrong_passwords(client, monkeypatch):
    monkeypatch.setattr("nagme.app.asyncio.sleep", _no_sleep)
    for _ in range(5):
        assert login(client, "guess").status_code == 401
    response = login(client)  # even the right password is refused while locked
    assert response.status_code == 429
    assert "Too many wrong passwords" in response.text


def test_lockout_expires():
    now = [0.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    for _ in range(5):
        limiter.fail("1.2.3.4")
    assert limiter.locked("1.2.3.4") and not limiter.locked("5.6.7.8")
    now[0] += 15 * 60 + 1
    assert not limiter.locked("1.2.3.4")


async def _no_sleep(_seconds):
    return None


# Sessions


def test_sign_out_everywhere_kills_other_sessions(app, signed_in):
    from fastapi.testclient import TestClient

    with TestClient(app) as other:
        login(other)
        assert other.get("/", follow_redirects=False).status_code == 200
        signed_in.post("/logout-everywhere")
        assert other.get("/", follow_redirects=False).status_code == 303
        assert signed_in.get("/", follow_redirects=False).status_code == 303


def test_login_issues_a_fresh_session(client):
    before = csrf_from(client, "/login")
    login(client)
    assert csrf_from(client) != before


# Replays of captured Twilio webhooks


def test_replayed_text_runs_only_once(client, manager, parsed):
    turn_on(manager)
    parsed["action"] = {"action": "commit", "what": "Deck", "minutes": 30, "due_time": None,
                        "commitment_id": None}  # fmt: skip
    params = {"From": PHONE, "Body": "deck, 30 min", "MessageSid": "SM123"}
    assert "Got it" in twilio_post(client, "/twilio/sms", params).text
    assert "<Message>" not in twilio_post(client, "/twilio/sms", params).text
    assert len(manager.db.open_commitments()) == 1


def test_replayed_inbound_call_is_refused(client, manager):
    turn_on(manager)
    params = {"From": PHONE, "CallSid": "CA42"}
    assert "<Stream" in twilio_post(client, "/twilio/inbound", params).text
    manager.db.update_call(1, status="completed")
    assert "<Stream" not in twilio_post(client, "/twilio/inbound", params).text


def test_spoofed_caller_id_is_refused(client, manager):
    turn_on(manager)
    params = {"From": PHONE, "CallSid": "CA43", "StirVerstat": "TN-Validation-Failed-C"}
    assert "<Stream" not in twilio_post(client, "/twilio/inbound", params).text


# Privacy


def test_old_transcripts_are_deleted_but_summaries_kept(manager, clock):
    turn_on(manager)
    call = manager.db.create_call("manual", clock.now)
    manager.db.update_call(
        call.id,
        status="completed",
        ended_at=clock.now,
        summary="Kept",
        transcript=[{"role": "user", "text": "private"}],
    )
    clock.now += timedelta(days=91)
    import asyncio

    asyncio.run(manager.tick())
    saved = manager.db.get_call(call.id)
    assert saved.transcript == [] and saved.summary == "Kept"


def test_text_contents_never_reach_the_logs(client, manager, parsed, caplog):
    turn_on(manager)
    parsed["action"] = {"action": "plan", "what": "secret plan", "minutes": None,
                        "due_time": None, "commitment_id": None}  # fmt: skip
    with caplog.at_level("INFO"):
        twilio_post(
            client, "/twilio/sms", {"From": PHONE, "Body": "my secret plan", "MessageSid": "SM9"}
        )
    assert "secret" not in caplog.text
