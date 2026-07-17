from __future__ import annotations

from datetime import datetime, timezone


def format_timestamp(value: object) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    parsed = parsed.astimezone(timezone.utc)
    month = parsed.strftime("%b")
    hour = parsed.strftime("%I").lstrip("0") or "0"
    return f"{month} {parsed.day}, {parsed.year} {hour}:{parsed:%M} {parsed:%p} UTC"


def humanize_status(value: object) -> str:
    if value in (None, ""):
        return ""
    text = str(value).replace("_", " ").strip()
    if not text:
        return ""
    if text.islower() or " " in text:
        return " ".join(word.capitalize() for word in text.split())
    return text
