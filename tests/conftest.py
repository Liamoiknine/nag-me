from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from nagme.app import create_app
from nagme.config import Config

AUTH_TOKEN = "test-auth-token"
PUBLIC_URL = "https://nagme.example.test"


class FakeTelephony:
    def __init__(self) -> None:
        self.placed: list[tuple[str, int]] = []
        self.hung_up: list[str] = []
        self.fail_with: Exception | None = None

    def place_call(self, to: str, call_id: int) -> str:
        if self.fail_with:
            raise self.fail_with
        self.placed.append((to, call_id))
        return f"CA{call_id:032d}"

    def hang_up(self, call_sid: str) -> None:
        self.hung_up.append(call_sid)


class FakeRealtime:
    """Plays the OpenAI side of a call: greets, listens, then wraps up.

    After `listen_chunks` audio chunks from the caller it acts as if they finished a
    sentence: commits it, transcribes it, says goodbye and calls finish_call.
    """

    def __init__(self, listen_chunks: int = 3, barge_in: bool = False) -> None:
        self.sent: list[dict] = []
        self.listen_chunks = listen_chunks
        self.barge_in = barge_in
        self.audio_in = 0
        self.queue: asyncio.Queue | None = None

    def emit(self, **event) -> None:
        assert self.queue is not None
        self.queue.put_nowait(event)

    async def send(self, message: str) -> None:
        event = json.loads(message)
        self.sent.append(event)
        kind = event["type"]
        if kind == "response.create" and "response" in event:  # the greeting
            self.emit(type="response.output_audio.delta", item_id="item_greet", delta="AAAA")
            self.emit(
                type="response.output_audio_transcript.done",
                item_id="item_greet",
                transcript="Morning. What's the plan today?",
            )
            if self.barge_in:
                self.emit(type="input_audio_buffer.speech_started", item_id="item_user1")
        elif kind == "input_audio_buffer.append":
            self.audio_in += 1
            if self.audio_in == self.listen_chunks:
                self.emit(type="input_audio_buffer.committed", item_id="item_user1")
                self.emit(type="response.output_audio.delta", item_id="item_bye", delta="BBBB")
                self.emit(
                    type="response.output_audio_transcript.done",
                    item_id="item_bye",
                    transcript="Good. Go get it done.",
                )
                self.emit(
                    type="conversation.item.input_audio_transcription.completed",
                    item_id="item_user1",
                    transcript="Finishing the quarterly report.",
                )
                self.emit(
                    type="response.function_call_arguments.done",
                    name="finish_call",
                    call_id="fc_1",
                    arguments=json.dumps(
                        {
                            "summary": "Working on the quarterly report.",
                            "on_track": "yes",
                            "plan": "Finish the quarterly report by 3pm.",
                        }
                    ),
                )

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        assert self.queue is not None
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        twilio_account_sid="ACtest",
        twilio_auth_token=AUTH_TOKEN,
        twilio_phone_number="+15550001111",
        openai_api_key="sk-test",
        public_url=PUBLIC_URL,
        admin_password="hunter2",
        secret_key="test-secret",
        database_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
def telephony() -> FakeTelephony:
    return FakeTelephony()


@pytest.fixture
def realtime() -> FakeRealtime:
    return FakeRealtime()


@pytest.fixture
def clock() -> Clock:
    # Tuesday 2026-11-24, 10:00 in Chicago.
    return Clock(datetime(2026, 11, 24, 16, 0, tzinfo=UTC))


@pytest.fixture
def app(cfg, telephony, realtime, clock):
    @asynccontextmanager
    async def connect(_cfg):
        realtime.queue = asyncio.Queue()
        yield realtime

    application = create_app(cfg, telephony=telephony, openai_connect=connect, run_scheduler=False)
    application.state.manager.clock = clock
    return application


@pytest.fixture
def manager(app):
    return app.state.manager


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def signed_in(client):
    client.post("/login", data={"password": "hunter2"})
    return client


def twilio_post(client: TestClient, path: str, params: dict[str, str], *, sign: bool = True):
    headers = {}
    if sign:
        headers["X-Twilio-Signature"] = RequestValidator(AUTH_TOKEN).compute_signature(
            PUBLIC_URL + path, params
        )
    return client.post(path, data=params, headers=headers)
