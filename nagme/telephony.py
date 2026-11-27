"""Twilio: placing and ending calls, the TwiML that starts the audio stream, and
checking that webhooks really come from Twilio."""

from __future__ import annotations

from typing import Protocol

from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Connect, VoiceResponse

from .config import Config


class Telephony(Protocol):
    def place_call(self, to: str, call_id: int) -> str:
        """Start an outbound call and return its Twilio call SID."""
        ...

    def hang_up(self, call_sid: str) -> None: ...

    def configure_number(self, public_url: str) -> str:
        """Point the Nag Me number's incoming calls and texts at this server."""
        ...


class TwilioTelephony:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)
        self._is_trial: bool | None = None

    def is_trial(self) -> bool:
        if self._is_trial is None:
            account = self.client.api.accounts(self.cfg.twilio_account_sid).fetch()
            self._is_trial = account.type == "Trial"
        return self._is_trial

    def place_call(self, to: str, call_id: int) -> str:
        base = self.cfg.public_url
        # Voicemail detection, so a declined call counts as dodged. Not on trial
        # accounts: there you must press a key before the call connects, which voicemail
        # can't do, and detection then mistakes your "hello" for a machine.
        detection = {} if self.is_trial() else {"machine_detection": "Enable"}
        call = self.client.calls.create(
            to=to,
            from_=self.cfg.twilio_phone_number,
            url=f"{base}/twilio/voice?call_id={call_id}",
            method="POST",
            status_callback=f"{base}/twilio/status?call_id={call_id}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
            timeout=25,  # seconds of ringing before Twilio gives up
            **detection,
        )
        return call.sid

    def hang_up(self, call_sid: str) -> None:
        self.client.calls(call_sid).update(status="completed")

    def configure_number(self, public_url: str) -> str:
        numbers = self.client.incoming_phone_numbers.list(
            phone_number=self.cfg.twilio_phone_number, limit=1
        )
        if not numbers:
            return f"{self.cfg.twilio_phone_number} isn't a number on this Twilio account."
        numbers[0].update(
            voice_url=f"{public_url}/twilio/inbound",
            voice_method="POST",
            sms_url=f"{public_url}/twilio/sms",
            sms_method="POST",
        )
        return f"Incoming calls and texts to {self.cfg.twilio_phone_number} now reach this server."


class UnconfiguredTelephony:
    """Stands in until the Twilio settings are filled in, so the dashboard still works."""

    def place_call(self, to: str, call_id: int) -> str:
        raise RuntimeError(
            "Twilio isn't configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
            "TWILIO_PHONE_NUMBER in .env."
        )

    def hang_up(self, call_sid: str) -> None:
        pass

    def configure_number(self, public_url: str) -> str:
        return "Twilio isn't configured."


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
    if message:
        response.say(message)
    response.hangup()
    return str(response)


def message_twiml(text: str | None) -> str:
    response = MessagingResponse()
    if text:
        response.message(text)
    return str(response)


def is_valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    if not auth_token or not signature:
        return False
    return RequestValidator(auth_token).validate(url, params, signature)
