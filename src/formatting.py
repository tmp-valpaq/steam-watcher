"""Shared human-readable formatting helpers for user-facing messages.

These were previously duplicated across bot.py and watcher.py. They are
centralized here so both callers share one implementation. Behavior is
byte-identical to the originals — tests and message text depend on it.
"""


def format_playtime_minutes(minutes: int) -> str:
    """Format a playtime given in MINUTES to a 'Xч Yмин' string."""
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0 and mins > 0:
        return f"{hours}ч {mins}мин"
    elif hours > 0:
        return f"{hours}ч"
    else:
        return f"{mins}мин"


def format_duration_seconds(seconds: int) -> str:
    """Format a duration given in SECONDS to a human-readable string."""
    if seconds < 60:
        return f"{seconds}с"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}мин {secs}с"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}ч {mins}мин"
