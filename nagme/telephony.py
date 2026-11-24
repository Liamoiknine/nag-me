"""Twilio: placing and ending calls, the TwiML that starts the audio stream, and
checking that webhooks really come from Twilio."""

from __future__ import annotations

from typing import Protocol

from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from .config import Config


class Telephony(Protocol):
    def place_call(self, to: str, call_id: int) -> str:
        """Start an outbound call and return its Twilio call SID."""
        ...

    def hang_up(self, call_sid: str) -> None: ...


class TwilioTelephony:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)

    def place_call(self, to: str, call_id: int) -> str:
        base = self.cfg.public_url
        call = self.client.calls.create(
            to=to,
            from_=self.cfg.twilio_phone_number,
            url=f"{base}/twilio/voice?call_id={call_id}",
            method="POST",
            status_callback=f"{base}/twilio/status?call_id={call_id}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            timeout=25,  # seconds of ringing before Twilio gives up
        )
        return call.sid

    def hang_up(self, call_sid: str) -> None:
        self.client.calls(call_sid).update(status="completed")


class UnconfiguredTelephony:
    """Stands in until the Twilio settings are filled in, so the dashboard still works."""

    def place_call(self, to: str, call_id: int) -> str:
        raise RuntimeError(
            "Twilio isn't configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
            "TWILIO_PHONE_NUMBER in .env."
        )

    def hang_up(self, call_sid: str) -> None:
        pass


def stream_twiml(stream_url: str, call_id: int, token: str) -> str:
    """Connect the answered call to our media-stream WebSocket.

    Twilio drops query strings on stream URLs, so the call ID and its one-time token
    travel as stream parameters instead.
    """
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=stream_url)
    stream.parameter(name="call_id", value=str(call_id))
    stream.parameter(name="token", value=token)
    response.append(connect)
    return str(response)


def hangup_twiml(message: str) -> str:
    response = VoiceResponse()
    response.say(message)
    response.hangup()
    return str(response)


def is_valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    if not auth_token or not signature:
        return False
    return RequestValidator(auth_token).validate(url, params, signature)
