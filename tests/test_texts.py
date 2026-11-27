from tests.conftest import PHONE, turn_on, twilio_post


def text(client, body, sender=PHONE):
    return twilio_post(client, "/twilio/sms", {"From": sender, "Body": body}).text


def test_starting_a_session_by_text(client, manager, parsed, notes):
    turn_on(manager)
    parsed["action"] = {"action": "commit", "what": "Pitch deck", "minutes": 45,
                        "due_time": None, "commitment_id": None}  # fmt: skip
    reply = text(client, "starting the pitch deck, 45 min")
    assert "Got it: Pitch deck, due 10:45 AM." in reply
    [c] = manager.db.open_commitments()
    assert c.text == "Pitch deck" and c.source == "text"
    assert notes and "Pitch deck" in notes[-1][1]


def test_done_snooze_plan_and_status(client, manager, parsed):
    turn_on(manager)
    c = manager.add_commitment("Memo", due_in_minutes=30)
    parsed["action"] = {"action": "done", "what": "memo", "minutes": None, "due_time": None,
                        "commitment_id": c.id}  # fmt: skip
    assert "Marked done: Memo" in text(client, "done with memo")
    assert f"id {c.id}: Memo" in parsed["context"]

    parsed["action"] = {"action": "snooze", "what": None, "minutes": 30, "due_time": None,
                        "commitment_id": None}  # fmt: skip
    assert "no calls until 10:30 AM" in text(client, "busy 30")

    parsed["action"] = {"action": "plan", "what": "Ship deck, email Sam", "minutes": None,
                        "due_time": None, "commitment_id": None}  # fmt: skip
    text(client, "plan: ship deck, email Sam")
    parsed["action"] = {"action": "status", "what": None, "minutes": None, "due_time": None,
                        "commitment_id": None}  # fmt: skip
    assert "Plan: Ship deck, email Sam" in text(client, "status")


def test_texts_from_strangers_are_ignored(client, manager, parsed):
    turn_on(manager)
    parsed["action"] = {"action": "plan", "what": "hijack", "minutes": None, "due_time": None,
                        "commitment_id": None}  # fmt: skip
    reply = text(client, "plan: hijack", sender="+12125550199")
    assert "<Message>" not in reply
    assert manager.db.get_plan(manager.clock().date()) is None


def test_unsigned_texts_are_rejected(client):
    assert twilio_post(client, "/twilio/sms", {"From": PHONE, "Body": "hi"},
                       sign=False).status_code == 403  # fmt: skip
