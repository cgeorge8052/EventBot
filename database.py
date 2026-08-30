"""
SQLite persistence for the photo contest bot.

The `contests` table schema changed to support the voting phase (candidate
themes, poll message, vote-close time). Database.__init__ detects an old,
incompatible `contests` table (missing the `period_key` column) and
automatically renames that file out of the way with a `.pre-voting-schema.bak`
suffix before creating a fresh database - so upgrading never crashes, it
just starts a new history (the old file isn't deleted, just renamed).
"""

import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field

log = logging.getLogger("contest_bot.database")


@dataclass
class Contest:
    id: int
    period_key: str
    period_type: str  # "daily" or "monthly"
    candidate_themes: list[str]
    theme: str | None  # None until voting finishes
    status: str  # "voting" | "active" | "completed"
    start_at: str  # ISO datetime, period start
    vote_ends_at: str  # ISO datetime, when voting closes / submissions open
    end_at: str  # ISO datetime, period end / when submissions close
    poll_message_id: str | None = None
    poll_channel_id: str | None = None
    winner_user_ids: list[str] = field(default_factory=list)
    winner_message_id: str | None = None
    winner_reaction_count: int | None = None


@dataclass
class Entry:
    id: int
    contest_id: int
    message_id: str
    channel_id: str
    user_id: str
    created_at: str


class Database:
    def __init__(self, path: str):
        self.path = path
        self._backup_if_incompatible_schema()
        self._init_schema()

    def _backup_if_incompatible_schema(self) -> None:
        """If an old-schema contest.db (pre theme-voting) is sitting at
        self.path, move it aside so a fresh, compatible database can be
        created instead of crashing on a missing column."""
        if not os.path.exists(self.path):
            return
        try:
            with closing(sqlite3.connect(self.path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(contests)").fetchall()}
        except sqlite3.Error:
            return  # not a sqlite file / unreadable - let normal init surface the real error

        if not columns:
            return  # no `contests` table yet (fresh/empty file) - nothing to migrate

        if "period_key" in columns:
            return  # already the current schema

        backup_path = f"{self.path}.pre-voting-schema.bak"
        if os.path.exists(backup_path):
            os.remove(backup_path)
        os.rename(self.path, backup_path)
        log.warning(
            "Found an old-schema database at %s (from before theme voting was added). "
            "Moved it to %s and starting a fresh database - your old contest history is "
            "preserved in that backup file but won't be used going forward.",
            self.path, backup_path,
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_key TEXT NOT NULL,
                    period_type TEXT NOT NULL,
                    candidate_themes TEXT NOT NULL,
                    theme TEXT,
                    status TEXT NOT NULL DEFAULT 'voting',
                    start_at TEXT NOT NULL,
                    vote_ends_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    poll_message_id TEXT,
                    poll_channel_id TEXT,
                    winner_user_ids TEXT,
                    winner_message_id TEXT,
                    winner_reaction_count INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contests_period_key ON contests (period_key)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contest_id INTEGER NOT NULL,
                    message_id TEXT UNIQUE NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (contest_id) REFERENCES contests (id)
                )
                """
            )
            conn.commit()

    # -- contests -----------------------------------------------------------

    def create_voting_contest(
        self,
        period_key: str,
        period_type: str,
        candidate_themes: list[str],
        start_at: str,
        vote_ends_at: str,
        end_at: str,
    ) -> Contest:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO contests "
                "(period_key, period_type, candidate_themes, status, start_at, vote_ends_at, end_at) "
                "VALUES (?, ?, ?, 'voting', ?, ?, ?)",
                (period_key, period_type, ",".join(candidate_themes), start_at, vote_ends_at, end_at),
            )
            conn.commit()
            return Contest(
                id=cur.lastrowid,
                period_key=period_key,
                period_type=period_type,
                candidate_themes=candidate_themes,
                theme=None,
                status="voting",
                start_at=start_at,
                vote_ends_at=vote_ends_at,
                end_at=end_at,
            )

    def get_latest_contest(self) -> Contest | None:
        """The most recently created round, regardless of status. This is
        'the current round' from the state machine's point of view."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM contests ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return self._row_to_contest(row) if row else None

    def get_contest_by_id(self, contest_id: int) -> Contest | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM contests WHERE id = ?", (contest_id,)
            ).fetchone()
            return self._row_to_contest(row) if row else None

    def set_poll_message(self, contest_id: int, message_id: str, channel_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE contests SET poll_message_id = ?, poll_channel_id = ? WHERE id = ?",
                (message_id, channel_id, contest_id),
            )
            conn.commit()

    def finalize_theme(self, contest_id: int, theme: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE contests SET theme = ?, status = 'active' WHERE id = ?",
                (theme, contest_id),
            )
            conn.commit()

    def complete_contest(
        self,
        contest_id: int,
        winner_user_ids: list[str],
        winner_message_id: str | None,
        winner_reaction_count: int | None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE contests SET status = 'completed', winner_user_ids = ?, "
                "winner_message_id = ?, winner_reaction_count = ? WHERE id = ?",
                (
                    ",".join(winner_user_ids) if winner_user_ids else None,
                    winner_message_id,
                    winner_reaction_count,
                    contest_id,
                ),
            )
            conn.commit()

    def get_recent_winning_themes(self, limit: int) -> list[str]:
        """Themes used by the most recent completed rounds, newest first."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT theme FROM contests WHERE status = 'completed' AND theme IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [row["theme"] for row in rows]

    def reset_leaderboard(self) -> int:
        """Clear win history for the all-time leaderboard by wiping
        winner_user_ids off every completed round. Round history (themes,
        entries, dates, winning message/reaction count) is left intact -
        only the leaderboard tally is affected. Returns the number of
        rounds that had their winner_user_ids cleared."""
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "UPDATE contests SET winner_user_ids = NULL "
                "WHERE status = 'completed' AND winner_user_ids IS NOT NULL"
            )
            conn.commit()
            return cur.rowcount

    def get_leaderboard(self, limit: int = 10) -> list[tuple[str, int]]:
        """(user_id, wins) pairs, most wins first. A tie-win counts for every
        tied winner, matching how rewards are distributed."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT winner_user_ids FROM contests "
                "WHERE status = 'completed' AND winner_user_ids IS NOT NULL"
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            for user_id in row["winner_user_ids"].split(","):
                if user_id:
                    counts[user_id] = counts.get(user_id, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    # -- entries --------------------------------------------------------------

    def add_entry(
        self, contest_id: int, message_id: str, channel_id: str, user_id: str, created_at: str
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO entries "
                "(contest_id, message_id, channel_id, user_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (contest_id, message_id, channel_id, user_id, created_at),
            )
            conn.commit()

    def user_has_entry(self, contest_id: int, user_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM entries WHERE contest_id = ? AND user_id = ? LIMIT 1",
                (contest_id, user_id),
            ).fetchone()
            return row is not None

    def get_entries_for_contest(self, contest_id: int) -> list[Entry]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM entries WHERE contest_id = ? ORDER BY id ASC",
                (contest_id,),
            ).fetchall()
            return [
                Entry(
                    id=r["id"],
                    contest_id=r["contest_id"],
                    message_id=r["message_id"],
                    channel_id=r["channel_id"],
                    user_id=r["user_id"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    @staticmethod
    def _row_to_contest(row: sqlite3.Row) -> Contest:
        return Contest(
            id=row["id"],
            period_key=row["period_key"],
            period_type=row["period_type"],
            candidate_themes=row["candidate_themes"].split(",") if row["candidate_themes"] else [],
            theme=row["theme"],
            status=row["status"],
            start_at=row["start_at"],
            vote_ends_at=row["vote_ends_at"],
            end_at=row["end_at"],
            poll_message_id=row["poll_message_id"],
            poll_channel_id=row["poll_channel_id"],
            winner_user_ids=row["winner_user_ids"].split(",") if row["winner_user_ids"] else [],
            winner_message_id=row["winner_message_id"],
            winner_reaction_count=row["winner_reaction_count"],
        )
