"""SQLite storage for Discord user <-> Google account links."""

from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

STATE_TTL_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    discord_id   INTEGER PRIMARY KEY,
    google_email TEXT NOT NULL UNIQUE,
    linked_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state      TEXT PRIMARY KEY,
    discord_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class EmailAlreadyLinked(Exception):
    """The Google account is already linked to a different Discord user."""


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- links -----------------------------------------------------------

    def get_email(self, discord_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT google_email FROM links WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return row[0] if row else None

    def all_links(self) -> dict[int, str]:
        return dict(self._conn.execute("SELECT discord_id, google_email FROM links"))

    def set_link(self, discord_id: int, email: str) -> str | None:
        """Link a Google account. Returns the previously linked email, if any."""
        email = email.strip().lower()
        owner = self._conn.execute(
            "SELECT discord_id FROM links WHERE google_email = ?", (email,)
        ).fetchone()
        if owner and owner[0] != discord_id:
            raise EmailAlreadyLinked(email)
        previous = self.get_email(discord_id)
        self._conn.execute(
            "INSERT INTO links (discord_id, google_email, linked_at) VALUES (?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET google_email = excluded.google_email, "
            "linked_at = excluded.linked_at",
            (discord_id, email, int(time.time())),
        )
        self._conn.commit()
        return previous if previous != email else None

    def remove_link(self, discord_id: int) -> str | None:
        previous = self.get_email(discord_id)
        self._conn.execute("DELETE FROM links WHERE discord_id = ?", (discord_id,))
        self._conn.commit()
        return previous

    # --- OAuth state -----------------------------------------------------

    def create_state(self, discord_id: int) -> str:
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        self._conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - STATE_TTL_SECONDS,))
        self._conn.execute(
            "INSERT INTO oauth_states (state, discord_id, created_at) VALUES (?, ?, ?)",
            (state, discord_id, now),
        )
        self._conn.commit()
        return state

    def peek_state(self, state: str) -> int | None:
        """Return the Discord user for a still-valid state without consuming it."""
        row = self._conn.execute(
            "SELECT discord_id, created_at FROM oauth_states WHERE state = ?", (state,)
        ).fetchone()
        if not row or row[1] < time.time() - STATE_TTL_SECONDS:
            return None
        return row[0]

    def consume_state(self, state: str) -> int | None:
        """Return the Discord user a state belongs to, and invalidate it."""
        discord_id = self.peek_state(state)
        self._conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        self._conn.commit()
        return discord_id
