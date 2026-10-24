#!/usr/bin/env python3
"""Deterministic slot-fit checking — exposed to the live Gemini agent as a tool call.

How it works
------------
1. At booking creation, Google Calendar is queried and ALL busy blocks from
   preferred_date → max_date are saved to:
       <VOXLAYER_DATA_DIR>/busy_cache/<booking_id>.json

2. During the live call, when the business proposes a time, Gemini calls
   `check_slot_fits(proposed_start_iso, proposed_end_iso)`.

3. This module loads the JSON file and does a simple overlap check:
       proposed_start < busy_end  AND  proposed_end > busy_start
   → fits=False if any busy block overlaps.
   → fits=True  if no conflicts and the date is within max_date.

No Google Calendar API calls happen during the call.
No "free window" computation. No gap arithmetic. Just: is this time busy?
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

LOGGER = logging.getLogger("bookapt.slot_matcher")


class SlotMatcher:
    def __init__(
        self,
        fitting_slots,          # kept for API compatibility (not used for checking)
        max_date: dt.date,
        booking_id: Optional[str] = None,
    ):
        self._max_date = max_date
        self._booking_id = booking_id

    # ------------------------------------------------------------------
    # Core check
    # ------------------------------------------------------------------

    def check(
        self,
        proposed_start: dt.datetime,
        proposed_end: dt.datetime,
    ) -> dict[str, Any]:
        """Return a JSON-able result dict for a proposed appointment slot.

        Checks (in order):
          1. Basic sanity (start before end)
          2. Hard deadline (proposed end must be on/before max_date)
          3. Busy-block overlap (load busy_cache JSON, simple overlap test)

        Returns a dict with keys: fits (bool), reason (str), conflict (dict|None)
        """
        # ── 1. Sanity ────────────────────────────────────────────────────────
        if proposed_start >= proposed_end:
            return {
                "fits": False,
                "reason": "invalid_range: start must be before end",
                "conflict": None,
            }

        # ── 2. Max-date ceiling ───────────────────────────────────────────────
        if proposed_end.date() > self._max_date:
            return {
                "fits": False,
                "reason": f"exceeds_deadline: latest allowed date is {self._max_date}",
                "conflict": None,
            }

        # ── 3. Busy-block check ───────────────────────────────────────────────
        if self._booking_id:
            try:
                from . import busy_store
                if busy_store.exists(self._booking_id):
                    overlaps, block = busy_store.has_overlap(
                        self._booking_id, proposed_start, proposed_end
                    )
                    if overlaps:
                        return {
                            "fits": False,
                            "reason": (
                                f"conflicts_with_existing_event: "
                                f"{block.get('start','?')} → {block.get('end','?')}"
                            ),
                            "conflict": block,
                        }
                    # Cache exists and slot is free
                    return {
                        "fits": True,
                        "reason": "slot_is_free",
                        "conflict": None,
                    }
            except Exception as exc:
                LOGGER.warning(
                    "busy_store check failed for booking %s: %s — accepting slot",
                    self._booking_id, exc,
                )

        # ── No cache (non-calendar booking) → accept within deadline ─────────
        return {
            "fits": True,
            "reason": "no_calendar_connected_slot_accepted",
            "conflict": None,
        }

    # ------------------------------------------------------------------
    # Tool interface (called from bridge.py)
    # ------------------------------------------------------------------

    def resolve_tool_call(self, args: dict[str, Any]) -> dict[str, Any]:
        """Parse ISO datetime strings from Gemini and return the check result."""
        try:
            start = dt.datetime.fromisoformat(args["proposed_start_iso"])
            end   = dt.datetime.fromisoformat(args["proposed_end_iso"])
        except (KeyError, ValueError) as exc:
            return {"fits": False, "reason": f"parse_error: {exc}", "conflict": None}

        # Gemini sometimes sends UTC-aware strings (e.g. "...Z").
        # Busy-store values are naive LOCAL datetimes, so convert first.
        if start.tzinfo is not None:
            start = start.astimezone().replace(tzinfo=None)
        if end.tzinfo is not None:
            end = end.astimezone().replace(tzinfo=None)

        try:
            return self.check(start, end)
        except Exception as exc:
            return {"fits": False, "reason": f"internal_error: {exc}", "conflict": None}

    @staticmethod
    def tool_schema() -> dict[str, Any]:
        """Gemini Live function-declaration schema for check_slot_fits."""
        return {
            "name": "check_slot_fits",
            "description": (
                "Check whether a proposed appointment date and time is free on "
                "the client's calendar and within their deadline. "
                "Call this EVERY TIME the business proposes a specific time before "
                "accepting or rejecting it. Never decide availability yourself — "
                "always call this tool and follow its result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposed_start_iso": {
                        "type": "string",
                        "description": (
                            "ISO 8601 datetime of the proposed appointment start "
                            "(e.g. '2026-10-31T10:00:00')."
                        ),
                    },
                    "proposed_end_iso": {
                        "type": "string",
                        "description": (
                            "ISO 8601 datetime of the proposed appointment end "
                            "(e.g. '2026-10-31T10:45:00')."
                        ),
                    },
                },
                "required": ["proposed_start_iso", "proposed_end_iso"],
            },
        }
