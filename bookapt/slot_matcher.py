#!/usr/bin/env python3
"""Deterministic slot-fit checking, exposed to the live model as a tool call.

This exists specifically so the accept/reject decision during a live
negotiation call is never left to LLM judgment. The calendar layer
precomputes a list of windows that already satisfy the required duration
(`BookingRequest.fitting_slots`); this module just does plain interval
containment checks against that in-memory list, plus a max-date ceiling
check. No network calls, no Google Calendar access here.

Usage in the bridge:
    matcher = SlotMatcher(booking.fitting_slots, booking.max_date)
    # register `matcher.tool_schema()` as a Gemini Live function declaration
    # when the model calls it, resolve with `matcher.check(proposed_start, proposed_end)`
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from .models import SlotWindow


@dataclass
class SlotCheckResult:
    fits: bool
    reason: str
    matched_window: Optional[SlotWindow] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fits": self.fits,
            "reason": self.reason,
            "matched_window": self.matched_window.to_dict() if self.matched_window else None,
        }


class SlotMatcher:
    def __init__(self, fitting_slots: list[SlotWindow], max_date: dt.date):
        # Sorted for cheap sequential scanning; lists here are expected to be
        # small (a handful of calendar windows), so no interval-tree needed.
        self._slots = sorted(fitting_slots, key=lambda s: s.start)
        self._max_date = max_date

    def check(self, proposed_start: dt.datetime, proposed_end: dt.datetime) -> SlotCheckResult:
        """Return whether [proposed_start, proposed_end) is auto-acceptable.

        Rule (per BookAppt.ai negotiation spec):
          - proposed_end must be on/before max_date, AND
          - the full proposed window must fall within one of the
            precomputed fitting_slots (business offering a time that's
            only partially free doesn't count).
        Price is never part of this check by design.
        """
        if proposed_start >= proposed_end:
            return SlotCheckResult(fits=False, reason="invalid_time_range: start is not before end")

        if proposed_end.date() > self._max_date:
            return SlotCheckResult(
                fits=False,
                reason=f"outside_max_date: proposed end {proposed_end.date()} is after max_date {self._max_date}",
            )

        for window in self._slots:
            if proposed_start >= window.start and proposed_end <= window.end:
                return SlotCheckResult(fits=True, reason="fits_within_calendar_window", matched_window=window)

        return SlotCheckResult(
            fits=False,
            reason="no_calendar_window_contains_this_slot",
        )

    def has_any_future_room(self) -> bool:
        """Cheap sanity check: are there any slots left at all before giving up."""
        return len(self._slots) > 0

    @staticmethod
    def tool_schema() -> dict[str, Any]:
        """Gemini Live function-declaration schema for this tool.

        Wire this into the `tools` config of the Live session alongside
        `response_modalities`, the same place gemini_bridge.py sets
        `system_instruction` / `speech_config`.
        """
        return {
            "name": "check_slot_fits",
            "description": (
                "Deterministically check whether a proposed appointment time "
                "is acceptable, given the client's calendar availability and "
                "maximum acceptable date. Always call this before verbally "
                "accepting or rejecting any time the business proposes. Do "
                "not reason about calendar fit yourself — always defer to "
                "this tool's result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposed_start_iso": {
                        "type": "string",
                        "description": "ISO 8601 datetime of the proposed appointment start.",
                    },
                    "proposed_end_iso": {
                        "type": "string",
                        "description": "ISO 8601 datetime of the proposed appointment end.",
                    },
                },
                "required": ["proposed_start_iso", "proposed_end_iso"],
            },
        }

    def resolve_tool_call(self, args: dict[str, Any]) -> dict[str, Any]:
        """Parse tool-call args from the live model and return a JSON-able result."""
        try:
            start = dt.datetime.fromisoformat(args["proposed_start_iso"])
            end = dt.datetime.fromisoformat(args["proposed_end_iso"])
        except (KeyError, ValueError) as exc:
            return {"fits": False, "reason": f"could_not_parse_times: {exc}"}
        return self.check(start, end).to_dict()
