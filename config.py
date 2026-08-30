"""
Loads all bot configuration from environment variables.

Create a `.env` file (see .env.example) or set these in your shell/host
environment before running bot.py.
"""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file; real env vars still work


def _get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


# --- Required ---------------------------------------------------------

DISCORD_BOT_TOKEN: str = _get_env("DISCORD_BOT_TOKEN", required=True)

CONTEST_CHANNEL_ID: int = int(_get_env("CONTEST_CHANNEL_ID", required=True))

# Comma-separated list of themes, e.g. "Sunsets,Pets,Street Photography,Macro,Black & White"
_raw_themes = _get_env("CONTEST_THEMES", required=True)
CONTEST_THEMES: list[str] = [t.strip() for t in _raw_themes.split(",") if t.strip()]
if len(CONTEST_THEMES) < 2:
    raise RuntimeError(
        "CONTEST_THEMES must contain at least 2 themes so there's something to vote between."
    )

# --- Optional -----------------------------------------------------------

# "daily" or "monthly". Controls how long one contest round lasts. Use
# "daily" for quick end-to-end testing; "monthly" is the real cadence.
CONTEST_PERIOD: str = _get_env("CONTEST_PERIOD", default="monthly").strip().lower()
if CONTEST_PERIOD not in ("daily", "monthly"):
    raise RuntimeError('CONTEST_PERIOD must be "daily" or "monthly"')

# How long, in minutes, the theme vote stays open at the start of each
# round before the winning theme is locked in and photo submissions open.
# Default 60 = "first hour". Capped to the round length itself.
VOTE_WINDOW_MINUTES: int = int(_get_env("VOTE_WINDOW_MINUTES", default="60"))

# How many random candidate themes are put to a vote each round. Capped to
# 10 (one per number emoji) and to the size of CONTEST_THEMES.
VOTE_CANDIDATE_COUNT: int = min(
    10, len(CONTEST_THEMES), int(_get_env("VOTE_CANDIDATE_COUNT", default="3"))
)

# How often (in seconds) the bot checks whether it's time to start a new
# round, close voting, or tally the winner. Cheap local DB reads, so a
# short interval is fine even for a monthly-cadence contest.
CHECK_INTERVAL_SECONDS: int = int(_get_env("CHECK_INTERVAL_SECONDS", default="30"))

# Role assigned to the current winner(s); removed from the previous winner(s).
_winner_role_raw = _get_env("WINNER_ROLE_ID")
WINNER_ROLE_ID: int | None = int(_winner_role_raw) if _winner_role_raw else None

# Free-form text describing the reward, shown in the winner announcement embed.
REWARD_DESCRIPTION: str = _get_env(
    "REWARD_DESCRIPTION", default="Bragging rights and the Contest Champion role!"
)

# IANA timezone name used to decide what "the 1st" / "the last day" of the
# month means, and when the daily check runs. Must be a full IANA zone name
# like "America/Los_Angeles" or "America/New_York" - NOT a short code like
# "PST" or "EST", which zoneinfo does not understand. Full list of valid
# names: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
_tz_name = _get_env("CONTEST_TIMEZONE", default="UTC")
try:
    CONTEST_TIMEZONE: ZoneInfo = ZoneInfo(_tz_name)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(
        f"CONTEST_TIMEZONE={_tz_name!r} is not a valid IANA timezone name. "
        f"Use a full name like 'America/Los_Angeles' or 'America/New_York', "
        f"not a short code like 'PST' or 'EST'. If this error mentions a "
        f"missing 'tzdata' module (common on Windows), run: "
        f"pip install tzdata"
    ) from exc

# How many previous winning themes to exclude when picking a new theme.
THEME_COOLDOWN: int = int(_get_env("THEME_COOLDOWN", default="3"))

# Emoji(s) counted as a "heart" vote. Defaults to the standard red heart.
# Override with a comma-separated list, e.g. HEART_EMOJIS="❤️,❤,💖" to also
# count other heart variants as votes.
_raw_heart_emojis = _get_env("HEART_EMOJIS", default="❤️,❤")
HEART_EMOJIS: set[str] = {e.strip() for e in _raw_heart_emojis.split(",") if e.strip()}

# Path to the SQLite database file.
DATABASE_PATH: str = _get_env("DATABASE_PATH", default="contest.db")
