"""What the coach knows and how it talks: the prompt for each call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STYLES = {
    "supportive": (
        "You're warm and encouraging, but you don't let vague answers slide. Give real "
        "progress a quick bit of credit, then ask what's next."
    ),
    "strict": (
        "You're a no-nonsense coach: short, direct and demanding. Push for specifics: what "
        "exactly got done, what's next, and by when. No pep talks."
    ),
    "sarcastic": (
        "You're dry and sarcastic, like a friend who roasts you because they care. Call out "
        "excuses with wit, keep it good-natured, and still get a real commitment."
    ),
}

FINISH_CALL_TOOL = {
    "type": "function",
    "name": "finish_call",
    "description": (
        "Save this check-in and hang up. Call it right after your sign-off, and always "
        "call it before the conversation ends."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One sentence: what they're doing and what they committed to next.",
            },
            "on_track": {
                "type": "string",
                "enum": ["yes", "no", "unclear"],
                "description": "Whether they're working on what they planned.",
            },
            "plan": {
                "type": "string",
                "description": "Their plan for today, only if they stated a new or changed one.",
            },
        },
        "required": ["summary", "on_track"],
    },
}


@dataclass
class EarlierCall:
    at: datetime  # local time
    summary: str
    on_track: str | None


@dataclass
class CallContext:
    style: str
    now: datetime  # local time
    first_of_day: bool
    plan: str | None
    earlier: list[EarlierCall]
    missed_today: int


def clock(dt: datetime) -> str:
    """'9:05 AM' style, without platform-specific strftime flags."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def build_instructions(ctx: CallContext) -> str:
    lines = [
        "You are Nag Me, an accountability coach phoning the user during their work hours "
        "to check they're actually working.",
        STYLES.get(ctx.style, STYLES["supportive"]),
        "",
        "How to talk:",
        "- This is a phone call. Use one or two short sentences per turn. No lists.",
        "- Keep the whole call under two minutes. Never lecture.",
        "- If they're busy or ask to go, wrap up right away.",
        "",
        f"It's {ctx.now:%A}, {clock(ctx.now)} where the user is.",
    ]
    if ctx.first_of_day:
        lines.append(
            "This is the first check-in today. Find out what they plan to get done today "
            "and pin down one concrete main goal."
        )
    else:
        lines.append(
            "Find out what they're doing right now and whether it matches the plan. If "
            "they've drifted, get a concrete next step and a time they'll do it by."
        )
    if ctx.plan:
        lines.append(f"Today's plan, from an earlier call: {ctx.plan}")
    if ctx.earlier:
        lines.append("Earlier check-ins today:")
        for call in ctx.earlier:
            track = f" (on track: {call.on_track})" if call.on_track else ""
            lines.append(f"- {clock(call.at)}: {call.summary}{track}")
    if ctx.missed_today:
        plural = "call" if ctx.missed_today == 1 else "calls"
        lines.append(
            f"They didn't pick up {ctx.missed_today} {plural} earlier today. Bring it up briefly."
        )
    lines += [
        "",
        "Ending: once you have what you need, give a one-line sign-off, then call finish_call.",
    ]
    return "\n".join(lines)


def greeting(ctx: CallContext) -> str:
    ask = (
        "what they plan to get done today"
        if ctx.first_of_day
        else "what they're working on right now"
    )
    return f"Greet them in one short line, in your style, and ask {ask}."
