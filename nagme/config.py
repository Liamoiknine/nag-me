"""Runtime configuration, read from environment variables and a local .env file."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from dotenv import load_dotenv

# Environment variable -> Config attribute, for everything a real call needs.
REQUIRED = {
    "TWILIO_ACCOUNT_SID": "twilio_account_sid",
    "TWILIO_AUTH_TOKEN": "twilio_auth_token",
    "TWILIO_PHONE_NUMBER": "twilio_phone_number",
    "OPENAI_API_KEY": "openai_api_key",
    "PUBLIC_URL": "public_url",
    "ADMIN_PASSWORD": "admin_password",
}


@dataclass(frozen=True)
class Config:
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    openai_api_key: str = ""
    public_url: str = ""
    admin_password: str = ""
    secret_key: str = ""
    openai_model: str = "gpt-realtime-mini"
    openai_voice: str = "marin"
    openai_transcribe_model: str = "gpt-4o-mini-transcribe"
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    database_path: str = "nagme.db"
    max_call_seconds: int = 300

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()

        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        return cls(
            twilio_account_sid=env("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=env("TWILIO_AUTH_TOKEN"),
            twilio_phone_number=env("TWILIO_PHONE_NUMBER"),
            openai_api_key=env("OPENAI_API_KEY"),
            public_url=env("PUBLIC_URL").rstrip("/"),
            admin_password=env("ADMIN_PASSWORD"),
            # Without a fixed SECRET_KEY, logins simply reset when the server restarts.
            secret_key=env("SECRET_KEY") or secrets.token_urlsafe(32),
            openai_model=env("OPENAI_MODEL", cls.openai_model),
            openai_voice=env("OPENAI_VOICE", cls.openai_voice),
            openai_transcribe_model=env("OPENAI_TRANSCRIBE_MODEL", cls.openai_transcribe_model),
            openai_realtime_url=env("OPENAI_REALTIME_URL", cls.openai_realtime_url),
            database_path=env("DATABASE_PATH", cls.database_path),
            max_call_seconds=int(env("MAX_CALL_SECONDS", str(cls.max_call_seconds))),
        )

    def missing(self) -> list[str]:
        """Names of required environment variables that aren't set."""
        return [name for name, attr in REQUIRED.items() if not getattr(self, attr)]

    @property
    def stream_url(self) -> str:
        """The WebSocket URL Twilio streams call audio to."""
        base = self.public_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/twilio/stream"
