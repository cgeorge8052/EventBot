"""
Pure(ish) contest logic, kept separate from bot.py so it can be tested or
reused without spinning up a live Discord connection.

A contest "round" now has three phases:
  1. voting   - a handful of random candidate themes are posted; members
                vote by reacting with the matching number emoji.
  2. active   - the theme with the most votes has been chosen; members
                submit photos in the contest channel.
  3. completed - the round's entries have been tallied and a winner (or
                 co-winners, on a tie) declared.

A "period" is a calendar day, a Sunday-through-Saturday week, or a calendar
month (config.CONTEST_PERIOD) - daily is meant for quick end-to-end testing,
weekly and monthly are real cadences.
"""

import calendar
import random
from datetime import datetime, timedelta


def period_key(now: datetime, period_type: str) -> str:
    """Stable id for 'this round' - e.g. '2026-08-15' (daily), '2026-W35'
    (weekly, the Sunday the week starts on), or '2026-08' (monthly). Used
    to detect when a new round should start."""
    if period_type == "daily":
        return now.strftime("%Y-%m-%d")
    if period_type == "weekly":
        week_start, _ = _weekly_bounds(now)
        return week_start.strftime("%Y-%m-%d")
    return now.strftime("%Y-%m")


def _weekly_bounds(now: datetime) -> tuple[datetime, datetime]:
    """(start, end) of the Sunday-through-Saturday week containing `now`.
    Python's weekday() is Mon=0..Sun=6, so Sunday is 6; days_since_sunday
    turns that into Sun=0..Sat=6 so the week always starts on a Sunday."""
    days_since_sunday = (now.weekday() + 1) % 7
    start = (now - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = (start + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def period_bounds(now: datetime, period_type: str) -> tuple[datetime, datetime]:
    """(start, end) timestamps of the current period, in `now`'s tzinfo."""
    if period_type == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif period_type == "weekly":
        start, end = _weekly_bounds(now)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = calendar.monthrange(now.year, now.month)[1]
        end = start.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def vote_window_end(start: datetime, end: datetime, minutes: int) -> datetime:
    """When theme voting closes and photo submissions open - `minutes`
    after the period starts, but never later than the period itself ends."""
    return min(start + timedelta(minutes=minutes), end)


def pick_candidate_themes(pool: list[str], recent_winning_themes: list[str], count: int) -> list[str]:
    """Randomly pick up to `count` themes to put to a vote, preferring
    ones that haven't won recently. Falls back to recently-used themes
    only if the pool isn't big enough to avoid them entirely."""
    count = max(1, min(count, len(pool)))
    excluded = set(recent_winning_themes)
    eligible = [t for t in pool if t not in excluded]
    random.shuffle(eligible)
    candidates = eligible[:count]
    if len(candidates) < count:
        remaining_pool = [t for t in pool if t not in candidates]
        random.shuffle(remaining_pool)
        candidates += remaining_pool[: count - len(candidates)]
    return candidates


def tally_theme_votes(vote_counts: dict[str, int]) -> str:
    """vote_counts: theme -> reaction count. Returns the winning theme;
    ties (including an all-zero-votes round) are broken randomly."""
    if not vote_counts:
        raise ValueError("vote_counts must not be empty")
    top = max(vote_counts.values())
    tied = [theme for theme, c in vote_counts.items() if c == top]
    return random.choice(tied)


def tally_winners(
    entry_heart_counts: dict[str, tuple[str, int]],
) -> tuple[list[str], str | None, int]:
    """
    entry_heart_counts: message_id -> (user_id, heart_count)

    Returns (winning_user_ids, one_representative_winning_message_id, top_count).
    Multiple users tie if they share the single highest heart count on any
    one entry. If there are no entries at all, returns ([], None, 0).
    """
    if not entry_heart_counts:
        return [], None, 0

    top_count = max(count for _, count in entry_heart_counts.values())
    if top_count <= 0:
        return [], None, 0

    winning_entries = [
        (message_id, user_id)
        for message_id, (user_id, count) in entry_heart_counts.items()
        if count == top_count
    ]
    # De-duplicate users who might have multiple tied top entries.
    seen_users: list[str] = []
    for _, user_id in winning_entries:
        if user_id not in seen_users:
            seen_users.append(user_id)

    representative_message_id = winning_entries[0][0]
    return seen_users, representative_message_id, top_count
