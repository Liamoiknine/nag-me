"""The live conversation: relays phone audio between Twilio and OpenAI's realtime voice model.

Twilio streams the caller's audio to us as 8 kHz G.711 mu-law, which the realtime API
accepts as-is (audio/pcmu), so audio is relayed without any conversion. The model
handles turn-taking (server-side voice activity detection), and you can interrupt it
mid-sentence: we cut its audio on the phone and tell the model how much you heard.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from fastapi import WebSocket
from websockets.asyncio.client import connect

from . import coach
from .calls import CallManager
from .config import Config
from .db import Call

log = logging.getLogger("nagme.bridge")

PLAYBACK_DRAIN_SECONDS = 10  # max wait for the sign-off to finish playing before hanging up


class RealtimeConnection(Protocol):
    async def send(self, message: str) -> None: ...
    def __aiter__(self) -> AsyncIterator[str]: ...


OpenAIConnector = Callable[[Config], AbstractAsyncContextManager[RealtimeConnection]]


@asynccontextmanager
async def connect_openai(cfg: Config) -> AsyncIterator[RealtimeConnection]:
    url = f"{cfg.openai_realtime_url}?model={cfg.openai_model}"
    headers = {"Authorization": f"Bearer {cfg.openai_api_key}"}
    async with connect(url, additional_headers=headers, max_size=None) as ws:
        yield ws


def session_update(cfg: Config, ctx: coach.CallContext) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": coach.build_instructions(ctx),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "noise_reduction": {"type": "near_field"},
                    # Semantic VAD waits for you to finish your thought rather than
                    # jumping in at the first pause or background noise.
                    "turn_detection": {"type": "semantic_vad", "eagerness": "low"},
                    "transcription": {"model": cfg.openai_transcribe_model, "language": "en"},
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": cfg.openai_voice},
            },
            "tools": [coach.finish_call_tool(ctx)],
            "tool_choice": "auto",
        },
    }


async def handle_media_stream(
    ws: WebSocket, manager: CallManager, openai_connect: OpenAIConnector
) -> None:
    await ws.accept()
    try:
        start = await asyncio.wait_for(_wait_for_start(ws), timeout=10)
    except (TimeoutError, json.JSONDecodeError):
        start = None
    params = (start or {}).get("customParameters") or {}
    call = manager.authorize_stream(params.get("call_id"), params.get("token")) if start else None
    if call is None:
        log.warning("Rejected a media stream with a missing or bad token")
        await ws.close(code=1008)
        return

    session = _Session(ws, start, call, manager)
    error = None
    try:
        async with openai_connect(manager.cfg) as oai:
            session.oai = oai
            await session.run()
    except Exception as exc:
        log.exception("Voice session for call %s failed", call.id)
        error = f"Voice session failed: {exc}"
        await manager.hang_up(session.call_sid)
    finally:
        manager.finish_stream(call.id, session.transcript(), error)
        log.info("Call %s ended", call.id)


async def _wait_for_start(ws: WebSocket) -> dict | None:
    async for raw in ws.iter_text():
        message = json.loads(raw)
        if message.get("event") == "start":
            return message["start"]
    return None


class _Session:
    def __init__(self, ws: WebSocket, start: dict, call: Call, manager: CallManager) -> None:
        self.ws = ws
        self.stream_sid: str = start["streamSid"]
        self.call_sid: str | None = start.get("callSid") or call.twilio_sid
        self.call = call
        self.manager = manager
        self.oai: RealtimeConnection | None = None

        # Barge-in bookkeeping: Twilio's media timestamps (ms into the call) tell us how
        # much of the model's current reply has actually played.
        self.latest_ts = 0
        self.reply_started_ts: int | None = None
        self.reply_item: str | None = None
        self.marks: list[str] = []
        self.mark_count = 0
        self.playback_idle = asyncio.Event()
        self.playback_idle.set()

        self.ending = False
        self._entries: list[dict[str, Any]] = []
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        ctx = self.manager.call_context(self.call)
        await self._to_openai(session_update(self.manager.cfg, ctx))
        await self._to_openai(
            {"type": "response.create", "response": {"instructions": coach.greeting(ctx)}}
        )

        tasks = [
            asyncio.create_task(self._from_twilio()),
            asyncio.create_task(self._from_openai()),
            asyncio.create_task(self._time_limit()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in [*tasks, *self._tasks]:
                task.cancel()
            await asyncio.gather(*tasks, *self._tasks, return_exceptions=True)
        for task in done:
            if task.exception():
                raise task.exception()

    def transcript(self) -> list[dict[str, str]]:
        return [
            {"role": e["role"], "text": e["text"].strip()}
            for e in self._entries
            if e.get("text") and e["text"].strip()
        ]

    # Twilio -> OpenAI

    async def _from_twilio(self) -> None:
        async for raw in self.ws.iter_text():
            message = json.loads(raw)
            event = message.get("event")
            if event == "media":
                self.latest_ts = int(message["media"].get("timestamp") or self.latest_ts)
                if not self.ending:
                    await self._to_openai(
                        {"type": "input_audio_buffer.append", "audio": message["media"]["payload"]}
                    )
            elif event == "mark":
                if self.marks:
                    self.marks.pop(0)
                if not self.marks:
                    self.playback_idle.set()
            elif event == "stop":
                return

    # OpenAI -> Twilio

    async def _from_openai(self) -> None:
        assert self.oai is not None
        async for raw in self.oai:
            event = json.loads(raw)
            kind = event.get("type")
            if kind in ("response.output_audio.delta", "response.audio.delta"):
                await self._play(event)
            elif kind == "input_audio_buffer.speech_started":
                await self._barge_in()
            elif kind == "input_audio_buffer.committed":
                self._entries.append({"role": "user", "item_id": event.get("item_id"), "text": ""})
            elif kind == "conversation.item.input_audio_transcription.completed":
                self._fill_user_text(event.get("item_id"), event.get("transcript", ""))
            elif kind in (
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            ):
                self._entries.append({"role": "coach", "text": event.get("transcript", "")})
            elif kind == "response.function_call_arguments.done":
                await self._function_call(event)
            elif kind == "error":
                log.error("OpenAI error on call %s: %s", self.call.id, event.get("error"))

    async def _play(self, event: dict) -> None:
        item = event.get("item_id")
        if item != self.reply_item or self.reply_started_ts is None:
            self.reply_item = item
            self.reply_started_ts = self.latest_ts
        await self.ws.send_json(
            {"event": "media", "streamSid": self.stream_sid, "media": {"payload": event["delta"]}}
        )
        self.mark_count += 1
        name = f"chunk-{self.mark_count}"
        await self.ws.send_json(
            {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}
        )
        self.marks.append(name)
        self.playback_idle.clear()

    async def _barge_in(self) -> None:
        """You started talking: stop the coach's audio and trim what you didn't hear."""
        if self.ending:
            return
        if self.marks and self.reply_started_ts is not None:
            if self.reply_item:
                await self._to_openai(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self.reply_item,
                        "content_index": 0,
                        "audio_end_ms": max(0, self.latest_ts - self.reply_started_ts),
                    }
                )
            await self.ws.send_json({"event": "clear", "streamSid": self.stream_sid})
            self.marks.clear()
            self.playback_idle.set()
        self.reply_item = None
        self.reply_started_ts = None

    async def _function_call(self, event: dict) -> None:
        output: dict[str, Any] = {"ok": True}
        if event.get("name") == "finish_call":
            try:
                args = json.loads(event.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            self.manager.save_outcome(
                self.call.id,
                summary=str(args.get("summary", "")),
                on_track=str(args.get("on_track", "unclear")),
                plan=args.get("plan"),
                follow_up_minutes=args.get("follow_up_minutes"),
            )
            self.ending = True
            task = asyncio.create_task(self._hang_up_after_playback())
            self._tasks.add(task)
        else:
            output = {"error": f"Unknown tool {event.get('name')!r}"}

        await self._to_openai(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": event.get("call_id"),
                    "output": json.dumps(output),
                },
            }
        )
        if not self.ending:
            await self._to_openai({"type": "response.create"})

    async def _hang_up_after_playback(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.playback_idle.wait(), PLAYBACK_DRAIN_SECONDS)
        await self.manager.hang_up(self.call_sid)

    async def _time_limit(self) -> None:
        await asyncio.sleep(self.manager.cfg.max_call_seconds)
        log.info("Call %s hit the %ss limit", self.call.id, self.manager.cfg.max_call_seconds)
        await self.manager.hang_up(self.call_sid)

    def _fill_user_text(self, item_id: str | None, text: str) -> None:
        for entry in reversed(self._entries):
            if entry["role"] == "user" and entry.get("item_id") == item_id:
                entry["text"] = text
                return
        self._entries.append({"role": "user", "item_id": item_id, "text": text})

    async def _to_openai(self, message: dict) -> None:
        assert self.oai is not None
        await self.oai.send(json.dumps(message))
