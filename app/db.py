#!/usr/bin/env python3
"""SQLite database layer for the BookAppt.ai host application.

All functions are plain Python — no Flask dependency — so they work equally
from the Flask request context and from the bridge process (run_bridge.py),
which is a separate asyncio process that needs booking_loader to read from
the same DB.

Schema:
    business_records  — saved reusable business entries (title, type, phone)
    bookings          — one row per booking attempt, linked to a record
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Resolve DB path from env (set by the host app or .env loader)."""
    return os.environ.get(
        "APP_DB_PATH",
        str(Path(__file__).parent.parent / "bookapt_data" / "bookapt.db"),
    )


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    target_type TEXT    NOT NULL,
    phone       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_business_phone ON business_records(phone);

CREATE TABLE IF NOT EXISTS bookings (
    id                    TEXT    PRIMARY KEY,
    business_record_id    INTEGER NOT NULL REFERENCES business_records(id),
    preferred_slot_start  TEXT,
    preferred_slot_end    TEXT,
    secondary_slot_start  TEXT,
    secondary_slot_end    TEXT,
    max_date              TEXT    NOT NULL,
    required_duration_min INTEGER NOT NULL,
    use_calendar          INTEGER NOT NULL DEFAULT 1,
    special_instructions  TEXT    NOT NULL DEFAULT '',
    fitting_slots_json    TEXT    NOT NULL DEFAULT '[]',
    status                TEXT    NOT NULL DEFAULT 'new',
    last_call_sid         TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _conn() as con:
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Business records
# ---------------------------------------------------------------------------

def create_business_record(title: str, target_type: str, phone: str) -> dict[str, Any]:
    """Insert a business record. If `phone` already exists, returns the existing row."""
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO business_records (title, target_type, phone) VALUES (?, ?, ?)",
            (title.strip(), target_type.strip(), phone.strip()),
        )
        row = con.execute(
            "SELECT * FROM business_records WHERE phone = ?", (phone.strip(),)
        ).fetchone()
        return dict(row)


def get_business_record(record_id: int) -> Optional[dict[str, Any]]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM business_records WHERE id = ?", (record_id,)
        ).fetchone()
        return dict(row) if row else None


def get_all_business_records() -> list[dict[str, Any]]:
    with _conn() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM business_records ORDER BY title COLLATE NOCASE"
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

def create_booking(
    booking_id: str,
    business_record_id: int,
    max_date: str,
    required_duration_min: int,
    fitting_slots: list[dict[str, Any]],
    preferred_slot_start: Optional[str] = None,
    preferred_slot_end: Optional[str] = None,
    secondary_slot_start: Optional[str] = None,
    secondary_slot_end: Optional[str] = None,
    use_calendar: bool = True,
    special_instructions: str = "",
) -> dict[str, Any]:
    fitting_json = json.dumps(fitting_slots, ensure_ascii=False)
    with _conn() as con:
        con.execute(
            """INSERT INTO bookings
               (id, business_record_id, preferred_slot_start, preferred_slot_end,
                secondary_slot_start, secondary_slot_end, max_date, required_duration_min,
                use_calendar, special_instructions, fitting_slots_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                booking_id,
                business_record_id,
                preferred_slot_start,
                preferred_slot_end,
                secondary_slot_start,
                secondary_slot_end,
                max_date,
                required_duration_min,
                1 if use_calendar else 0,
                special_instructions,
                fitting_json,
            ),
        )
        return _get_booking_joined(con, booking_id)


def get_booking(booking_id: str) -> Optional[dict[str, Any]]:
    with _conn() as con:
        return _get_booking_joined(con, booking_id)


def _get_booking_joined(con: sqlite3.Connection, booking_id: str) -> Optional[dict[str, Any]]:
    row = con.execute(
        """SELECT b.*, br.title, br.target_type, br.phone
           FROM bookings b
           JOIN business_records br ON br.id = b.business_record_id
           WHERE b.id = ?""",
        (booking_id,),
    ).fetchone()
    return dict(row) if row else None


def get_all_bookings() -> list[dict[str, Any]]:
    with _conn() as con:
        return [
            dict(r)
            for r in con.execute(
                """SELECT b.*, br.title, br.target_type, br.phone
                   FROM bookings b
                   JOIN business_records br ON br.id = b.business_record_id
                   ORDER BY b.created_at DESC"""
            ).fetchall()
        ]


def update_booking_status(
    booking_id: str,
    status: str,
    call_sid: Optional[str] = None,
) -> None:
    with _conn() as con:
        if call_sid is not None:
            con.execute(
                "UPDATE bookings SET status=?, last_call_sid=?, updated_at=datetime('now') WHERE id=?",
                (status, call_sid, booking_id),
            )
        else:
            con.execute(
                "UPDATE bookings SET status=?, updated_at=datetime('now') WHERE id=?",
                (status, booking_id),
            )
