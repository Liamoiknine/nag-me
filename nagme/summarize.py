"""A safety net: if a call ends without the coach saving it (no finish_call), a small
model summarizes the transcript so the call still gets a summary, verdict and plan."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx

from .config import Config

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence: what they're doing and what they committed to next.",
        },
        "on_track": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "plan": {
            "type": ["string", "null"],
            "description": "Their plan for today in one sentence, if they stated one.",
        },
    },
    "required": ["summary", "on_track", "plan"],
}

Summarizer = Callable[[list[dict[str, str]]], Awaitable[dict]]


def openai_summarizer(cfg: Config) -> Summarizer:
    async def summarize(transcript: list[dict[str, str]]) -> dict:
        text = "\n".join(f"{line['role']}: {line['text']}" for line in transcript)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
                json={
                    "model": cfg.openai_text_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Summarize this phone check-in between an "
                            "accountability coach and the user.",
                        },
                        {"role": "user", "content": text},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "call", "strict": True, "schema": SCHEMA},
                    },
                },
            )
            response.raise_for_status()
            return json.loads(response.json()["choices"][0]["message"]["content"])

    return summarize
