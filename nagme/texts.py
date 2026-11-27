"""Text commands: text the Nag Me number to start a session, report something done,
snooze, or set today's plan. A small OpenAI model turns free text into one action."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .calls import CallManager
from .config import Config
from .schedule import local_date

log = logging.getLogger("nagme.texts")

HELP = (
    "Text me things like: 'starting the deck, 45 min', 'memo by 3pm', 'done with memo', "
    "'busy 30', 'plan: ship the deck and email Sam', or 'status'."
)

ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["commit", "done", "snooze", "plan", "status", "help"],
            "description": "commit: they're starting or promising a task with a time. "
            "done: they finished something. snooze: busy, don't call for a while. "
            "plan: today's plan. status: what's next. help: anything else.",
        },
        "what": {
            "type": ["string", "null"],
            "description": "The task or plan as a short noun phrase, without filler like "
            "'starting' or 'working on' (e.g. 'Pitch deck').",
        },
        "minutes": {
            "type": ["integer", "null"],
            "description": "For commit: minutes from now it's due. For snooze: how long.",
        },
        "due_time": {
            "type": ["string", "null"],
            "description": "For commit: a clock time like 15:00 (24-hour), if they gave one.",
        },
        "commitment_id": {
            "type": ["integer", "null"],
            "description": "For done: which open commitment they mean.",
        },
    },
    "required": ["action", "what", "minutes", "due_time", "commitment_id"],
}

Parser = Callable[[str, str], Awaitable[dict[str, Any]]]


def openai_parser(cfg: Config) -> Parser:
    async def parse(body: str, context: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
                json={
                    "model": cfg.openai_text_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Turn a text message sent to a productivity coach into "
                            "one action.\n" + context,
                        },
                        {"role": "user", "content": body},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "action", "strict": True, "schema": ACTION_SCHEMA},
                    },
                },
            )
            response.raise_for_status()
            return json.loads(response.json()["choices"][0]["message"]["content"])

    return parse


async def handle_text(manager: CallManager, parse: Parser, body: str) -> str:
    """Run one text command and return the reply to send back."""
    s = manager.db.get_settings()
    now = manager.clock()
    open_items = manager.db.open_commitments()
    context = f"It's {manager.local_time(now)} for them. Open commitments:\n" + (
        "\n".join(f"- id {c.id}: {c.text}, due {manager.local_time(c.due_at)}" for c in open_items)
        or "- none"
    )
    try:
        action = await parse(body.strip()[:500], context)
    except Exception:
        log.exception("Couldn't parse a text command")
        return "Sorry, I couldn't read that. " + HELP
    log.info("Text command: %s", action.get("action"))  # never log message contents

    kind = action.get("action")
    if kind == "commit" and action.get("what"):
        try:
            c = manager.add_commitment(
                action["what"], action.get("minutes"), action.get("due_time"), source="text"
            )
        except ValueError:
            return f"How long will '{action['what']}' take? Reply like '{action['what']}, 45 min'."
        callback = "I'll call then to check." if s.active else "(Calls are paused.)"
        return f"Got it: {c.text}, due {manager.local_time(c.due_at)}. {callback}"
    if kind == "done":
        target = next((c for c in open_items if c.id == action.get("commitment_id")), None)
        target = target or (open_items[0] if len(open_items) == 1 else None)
        if target is None:
            return "Done with which one? " + _list(manager, open_items)
        manager.update_commitment(target.id, "done")
        return f"Nice. Marked done: {target.text}."
    if kind == "snooze":
        until = manager.snooze(action.get("minutes") or 30)
        return f"OK, no calls until {manager.local_time(until)} (except ones you ask for)."
    if kind == "plan" and action.get("what"):
        manager.db.set_plan(local_date(now, s), action["what"].strip()[:500])
        return "Plan saved for today."
    if kind == "status":
        plan = manager.db.get_plan(local_date(now, s))
        return (f"Plan: {plan}. " if plan else "No plan yet today. ") + _list(manager, open_items)
    return HELP


def _list(manager: CallManager, items: list) -> str:
    if not items:
        return "Nothing open."
    return "Open: " + "; ".join(f"{c.text} ({manager.local_time(c.due_at)})" for c in items[:5])
