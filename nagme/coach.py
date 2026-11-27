"""What the coach knows on each call, how it talks, and the tools it can use."""

from __future__ import annotations

from dataclasses import dataclass, field
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

DUE_RANGE = (5, 600)  # minutes from now a commitment can be due


@dataclass
class CommitmentView:
    id: int
    text: str
    due: datetime  # local time
    status: str  # open | done | missed | dropped


@dataclass
class EarlierCall:
    at: datetime  # local time
    summary: str
    on_track: str | None


@dataclass
class CallContext:
    purpose: str  # morning | deadline | drift | review | manual | inbound
    style: str
    now: datetime  # local time
    plan: str | None = None
    commitments: list[CommitmentView] = field(default_factory=list)  # today's
    due_now: list[CommitmentView] = field(default_factory=list)  # open and past due
    earlier: list[EarlierCall] = field(default_factory=list)
    missed_today: int = 0
    drift_reason: str | None = None
    drift_minutes_today: int | None = None
    calls_on: bool = True  # whether deadline calls will actually be placed

    @property
    def needs_plan(self) -> bool:
        return self.plan is None and self.purpose in ("morning", "manual", "inbound")


def clock(dt: datetime) -> str:
    """'9:05 AM' style, without platform-specific strftime flags."""
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


GOALS = {
    "morning": (
        "This is the morning planning call. Get the one to three things that matter most "
        "today, and a realistic finish time for each. Record each with add_commitment."
    ),
    "deadline": (
        "You're calling because something they committed to is due now (listed below). "
        "Ask if it's done. Mark each one with update_commitment: done, not_done with a new "
        "realistic time, or dropped if it no longer matters."
    ),
    "drift": (
        "You're calling because their Mac shows they've drifted off task (details below). "
        "Don't lecture. Ask what's going on, then get them back with one concrete next step "
        "and a time it'll be done by (add_commitment)."
    ),
    "review": (
        "This is the end-of-day review. Go through today's commitments quickly: what got "
        "done, what didn't and why, in a sentence each. Mark any still open with "
        "update_commitment. Then ask for the first thing they'll do tomorrow, and record it "
        "with add_commitment using due_time for tomorrow morning."
    ),
    "manual": (
        "This is an on-demand check-in. Find out what they're working on and whether it "
        "matches the plan. If there's no plan yet today, get one."
    ),
    "inbound": (
        "They called you. Usually they're starting a focus block: ask what they're "
        "starting and how long it'll take, record it with add_commitment, and get off the "
        "phone fast. If instead they're reporting something done, mark it."
    ),
}


def build_instructions(ctx: CallContext) -> str:
    lines = [
        "You are Nag Me, an accountability coach on the phone with the user during their "
        "work day. The point is to help them get their important work done, not to nag.",
        STYLES.get(ctx.style, STYLES["supportive"]),
        "",
        "How to talk:",
        "- This is a phone call. Use one or two short sentences per turn, and ask at most "
        "one question per turn. No lists.",
        "- Don't ask the same thing twice. If they've answered, move on.",
        '- If they say they need a moment to think, say only something like "Sure, take '
        'your time" and then wait silently until they speak.',
        "- If what they said sounds garbled or makes no sense, ask them to repeat it "
        "instead of guessing.",
        "- Push vague answers toward something concrete with a time, like 'draft the "
        "intro by 2:30'.",
        "- Keep the call under two minutes. If they're busy or ask to go, wrap up at once.",
        "",
        f"It's {ctx.now:%A}, {clock(ctx.now)} where the user is.",
        "",
        "Why you're calling: " + GOALS.get(ctx.purpose, GOALS["manual"]),
    ]
    if ctx.plan:
        lines.append(f"Today's plan: {ctx.plan}")
    if ctx.due_now:
        lines.append("Due now:")
        lines += [f"- [id {c.id}] {c.text} (due {clock(c.due)})" for c in ctx.due_now]
    due_ids = {c.id for c in ctx.due_now}
    others = [c for c in ctx.commitments if c.id not in due_ids]
    if others:
        lines.append("Today's other commitments:")
        lines += [f"- [id {c.id}] {c.text}, due {clock(c.due)}: {c.status}" for c in others]
    if ctx.drift_reason:
        lines.append(f"What the Mac saw: {ctx.drift_reason}")
    if ctx.drift_minutes_today:
        lines.append(f"Time on distracting sites or apps today: {ctx.drift_minutes_today} min.")
    if ctx.earlier:
        lines.append("Earlier calls today:")
        lines += [
            f"- {clock(c.at)}: {c.summary}" + (f" (on track: {c.on_track})" if c.on_track else "")
            for c in ctx.earlier
        ]
    if ctx.missed_today:
        plural = "call" if ctx.missed_today == 1 else "calls"
        lines.append(f"They dodged {ctx.missed_today} {plural} today. Mention it once, briefly.")
    lines.append("")
    if ctx.calls_on:
        lines.append(
            "When you record a commitment, tell them you'll call at that time to check. "
            "Don't promise any other callback."
        )
    else:
        lines.append(
            "Their scheduled calls are paused, so record commitments but don't promise to "
            "call back."
        )
    lines.append(
        "Ending: once you have what you need, give a one-line sign-off, then call finish_call."
    )
    return "\n".join(lines)


def greeting(ctx: CallContext) -> str:
    asks = {
        "morning": "what they need to get done today",
        "deadline": f"whether they finished: {ctx.due_now[0].text}" if ctx.due_now else "",
        "drift": "what they're up to right now",
        "review": "how the day went",
        "inbound": "what they're starting",
    }
    ask = asks.get(ctx.purpose) or "what they're working on right now"
    return f"Greet them in one short line, in your style, and ask {ask}."


def tools(ctx: CallContext) -> list[dict]:
    low, high = DUE_RANGE
    finish_properties: dict = {
        "summary": {
            "type": "string",
            "description": "One sentence: what they're doing and what they committed to next.",
        },
        "on_track": {
            "type": "string",
            "enum": ["yes", "no", "unclear"],
            "description": "Whether they're working on what they should be.",
        },
        "plan": {
            "type": "string",
            "description": (
                "Today's plan in one sentence."
                if ctx.needs_plan
                else "Today's plan, only if they stated a new or changed one."
            ),
        },
    }
    return [
        {
            "type": "function",
            "name": "add_commitment",
            "description": (
                "Record something they committed to finish by a specific time. They get a "
                "call at that time asking if it's done. Give due_in_minutes or due_time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "The task, in a few words."},
                    "due_in_minutes": {"type": "integer", "minimum": low, "maximum": high},
                    "due_time": {
                        "type": "string",
                        "description": "Local 24-hour time like 14:30. If it's already "
                        "past today, it means tomorrow.",
                    },
                },
                "required": ["what"],
            },
        },
        {
            "type": "function",
            "name": "update_commitment",
            "description": "Mark a commitment done, not done (optionally with a new time), "
            "or dropped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["done", "not_done", "dropped"]},
                    "new_due_in_minutes": {
                        "type": "integer",
                        "minimum": low,
                        "maximum": high,
                        "description": "For not_done: when it will be done instead.",
                    },
                },
                "required": ["id", "status"],
            },
        },
        {
            "type": "function",
            "name": "finish_call",
            "description": "Save this call and hang up. Call it right after your sign-off, "
            "and always before the conversation ends.",
            "parameters": {
                "type": "object",
                "properties": finish_properties,
                "required": ["summary", "on_track"] + (["plan"] if ctx.needs_plan else []),
            },
        },
    ]
