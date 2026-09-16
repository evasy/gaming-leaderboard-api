"""Time-windowed leaderboard keys.

Players care about "who is winning *today*", not only who has the highest
all-time score. Every submission fans out to one key per window; reads pick a
single window. Rolling windows carry a TTL so storage is bounded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum


class Window(StrEnum):
    ALL_TIME = "all_time"
    DAILY = "daily"
    WEEKLY = "weekly"


def window_suffix(window: Window, at: datetime | None = None) -> str:
    """Return the key suffix identifying the concrete bucket for `window`.

    All bucketing is done in UTC so that a deploy in another region cannot
    silently shift a player's "day".
    """
    if window is Window.ALL_TIME:
        return "all"
    moment = (at or datetime.now(UTC)).astimezone(UTC)
    if window is Window.DAILY:
        return f"d:{moment:%Y-%m-%d}"
    iso_year, iso_week, _ = moment.isocalendar()
    return f"w:{iso_year}-W{iso_week:02d}"


def ttl_for(window: Window, *, daily: int, weekly: int) -> int | None:
    """Retention for a bucket. `None` means "never expire"."""
    if window is Window.DAILY:
        return daily
    if window is Window.WEEKLY:
        return weekly
    return None
