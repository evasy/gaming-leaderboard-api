"""Unit tests for the pure helpers: window bucketing and rank assignment.

These have no I/O, so they pin down the trickiest logic in the service at
near-zero cost.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.windows import Window, ttl_for, window_suffix
from app.store.base import rank_page

# --- window bucketing ----------------------------------------------------


def test_all_time_bucket_is_constant() -> None:
    a = window_suffix(Window.ALL_TIME, datetime(2026, 1, 1, tzinfo=UTC))
    b = window_suffix(Window.ALL_TIME, datetime(2030, 6, 30, tzinfo=UTC))
    assert a == b == "all"


def test_daily_bucket_is_the_utc_date() -> None:
    assert window_suffix(Window.DAILY, datetime(2026, 9, 16, 13, 5, tzinfo=UTC)) == "d:2026-09-16"


def test_daily_bucket_rolls_over_at_utc_midnight() -> None:
    before = window_suffix(Window.DAILY, datetime(2026, 9, 16, 23, 59, 59, tzinfo=UTC))
    after = window_suffix(Window.DAILY, datetime(2026, 9, 17, 0, 0, 0, tzinfo=UTC))
    assert before == "d:2026-09-16"
    assert after == "d:2026-09-17"


def test_bucketing_is_utc_regardless_of_input_timezone() -> None:
    """A submission at 20:00 in New York belongs to the *next* UTC day."""
    local = datetime(2026, 9, 16, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    assert window_suffix(Window.DAILY, local) == "d:2026-09-17"


def test_weekly_bucket_uses_iso_weeks() -> None:
    assert window_suffix(Window.WEEKLY, datetime(2026, 9, 16, tzinfo=UTC)) == "w:2026-W38"


def test_weekly_bucket_is_stable_within_a_week_and_changes_across_one() -> None:
    monday = window_suffix(Window.WEEKLY, datetime(2026, 9, 14, tzinfo=UTC))
    sunday = window_suffix(Window.WEEKLY, datetime(2026, 9, 20, tzinfo=UTC))
    next_monday = window_suffix(Window.WEEKLY, datetime(2026, 9, 21, tzinfo=UTC))
    assert monday == sunday
    assert monday != next_monday


def test_iso_week_handles_the_year_boundary() -> None:
    """2026-12-31 is a Thursday, so ISO puts it in week 53 of 2026."""
    assert window_suffix(Window.WEEKLY, datetime(2026, 12, 31, tzinfo=UTC)) == "w:2026-W53"


def test_ttls_bound_rolling_windows_but_not_all_time() -> None:
    assert ttl_for(Window.ALL_TIME, daily=10, weekly=20) is None
    assert ttl_for(Window.DAILY, daily=10, weekly=20) == 10
    assert ttl_for(Window.WEEKLY, daily=10, weekly=20) == 20


# --- competition ranking -------------------------------------------------


def test_rank_page_on_distinct_scores() -> None:
    rows = [("a", 30), ("b", 20), ("c", 10)]
    assert [e.rank for e in rank_page(rows, start_index=0, rank_of_first=1)] == [1, 2, 3]


def test_rank_page_shares_ranks_within_ties() -> None:
    rows = [("a", 30), ("b", 30), ("c", 30), ("d", 10)]
    assert [e.rank for e in rank_page(rows, start_index=0, rank_of_first=1)] == [1, 1, 1, 4]


def test_rank_page_respects_a_mid_board_offset() -> None:
    """A page starting at offset 10 must report absolute ranks, not 1..n."""
    rows = [("k", 50), ("l", 40)]
    assert [e.rank for e in rank_page(rows, start_index=10, rank_of_first=11)] == [11, 12]


def test_rank_page_handles_a_tie_straddling_a_page_boundary() -> None:
    """The first row inherits the rank the backend computed, not its position."""
    rows = [("c", 30), ("d", 20)]
    ranks = [e.rank for e in rank_page(rows, start_index=2, rank_of_first=1)]
    assert ranks == [1, 4]


def test_rank_page_on_an_empty_page() -> None:
    assert rank_page([], start_index=0, rank_of_first=1) == []


# --- configuration -------------------------------------------------------


def test_cors_origins_accept_a_comma_separated_env_value() -> None:
    settings = Settings(cors_allow_origins="https://a.example, https://b.example")
    assert settings.cors_allow_origins == ["https://a.example", "https://b.example"]


def test_log_level_is_normalised() -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_invalid_backend_is_rejected_at_startup() -> None:
    """Misconfiguration should fail loudly at boot, not on first request."""
    with pytest.raises(ValidationError):
        Settings(store_backend="cassandra")
