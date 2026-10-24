#!/usr/bin/env python3
"""Per-booking busy-block cache — written at booking creation, used by check_slot_fits.

Why this exists
---------------
The original approach computed "free windows" from the calendar (gap arithmetic)
and checked whether a proposed time fell *inside* a free window.  That logic is
brittle: timezone offsets, multi-day gaps, and the UTC↔local conversion all
have many edge-cases that led to confirmed bookings landing on top of existing
calendar events.

This module flips the model:

  1. At booking creation  → query Google Calendar free/busy, convert the BUSY
     blocks to naive local datetimes, and write them to a small JSON file.

  2. During the live call → `has_overlap(booking_id, start, end)` loads that
     file and checks directly whether the proposed slot overlaps any busy block.
     No gap arithmetic, no business-hours filtering, no timezone conversion.

The JSON file is written to  <VOXLAYER_DATA_DIR>/busy_cache/<booking_id>.json
and is treated as disposable.  Losing it just means `has_overlap` returns
(False, None) and the slot_matcher falls back to the old fitting_slots logic.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger("bookapt.busy_store")

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    base = os.getenv("VOXLAYER_DATA_DIR", "voxlayer_data")
    path = Path(base) / "busy_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(booking_id: str) -> Path:
    return _cache_dir() / f"{booking_id}.json"


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save(
    booking_id: str,
    busy_blocks: list[tuple[dt.datetime, dt.datetime]],
    max_date: dt.date,
    timezone_name: str,
) -> None:
    """Persist busy blocks for a booking as naive-local ISO strings.

    Args:
        booking_id:    Unique ID of the booking.
        busy_blocks:   List of (start, end) naive-local datetimes representing
                       calendar busy periods, already converted from UTC.
        max_date:      The booking deadline (hard ceiling on accepted slots).
        timezone_name: IANA timezone name used for the conversion (recorded for
                       debugging; not used at lookup time).
    """
    data = {
        "booking_id": booking_id,
        "timezone": timezone_name,
        "max_date": max_date.isoformat(),
        "generated_at": dt.datetime.now().isoformat(),
        "busy_blocks": [
            {
                "start": s.replace(tzinfo=None).isoformat(),
                "end": e.replace(tzinfo=None).isoformat(),
            }
            for s, e in busy_blocks
        ],
    }
    path = _cache_path(booking_id)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    LOGGER.info(
        "busy_store: saved %d busy block(s) for booking %s → %s",
        len(busy_blocks), booking_id, path,
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load(booking_id: str) -> Optional[dict]:
    """Return the raw JSON dict for a booking, or None if not found."""
    path = _cache_path(booking_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("busy_store: failed to load %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Overlap check  (called from slot_matcher during the live call)
# ---------------------------------------------------------------------------

def has_overlap(
    booking_id: str,
    proposed_start: dt.datetime,
    proposed_end: dt.datetime,
) -> tuple[bool, Optional[dict]]:
    """Check whether a proposed naive-local slot overlaps any busy block.

    Returns:
        (True,  busy_block_dict)  if there IS an overlap.
        (False, None)             if there is NO overlap (slot is free).
        (False, None)             also if no cache file exists for this booking
                                  (caller should fall back to fitting_slots).

    The caller is responsible for ensuring proposed_start/end are naive local
    datetimes (same coordinate system as what was saved by `save()`).
    """
    data = load(booking_id)
    if data is None:
        # No cache → signal caller to use fallback logic
        return False, None

    for block in data.get("busy_blocks", []):
        try:
            b_start = dt.datetime.fromisoformat(block["start"])
            b_end = dt.datetime.fromisoformat(block["end"])
        except (KeyError, ValueError):
            continue

        # Standard overlap test: A overlaps B ⟺ A.start < B.end AND A.end > B.start
        if proposed_start < b_end and proposed_end > b_start:
            LOGGER.debug(
                "busy_store overlap: proposed [%s, %s] hits busy [%s, %s]",
                proposed_start, proposed_end, b_start, b_end,
            )
            return True, block

    return False, None


def exists(booking_id: str) -> bool:
    """Return True if a busy cache file exists for this booking."""
    return _cache_path(booking_id).exists()
