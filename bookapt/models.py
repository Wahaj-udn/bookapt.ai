#!/usr/bin/env python3
"""Shared data contract between voxlayer and the rest of BookAppt.ai.

This is the seam between the two halves of the project. The calendar/
dashboard layer produces a `BookingRequest`; voxlayer consumes it, places
call(s), and produces a `CallResult`. Nothing else needs to be shared.

Design notes
------------
- `fitting_slots` is a PRECOMPUTED list of calendar windows that already
  satisfy the user's required duration. voxlayer does not talk to Google
  Calendar at all — it only does deterministic interval matching against
  this list (see slot_matcher.py), exposed to the live model as a
  function/tool call during the conversation.
- `max_date` is a hard ceiling, not a soft preference. Any slot on/after
  today and on/before max_date that also fits `fitting_slots` is
  auto-acceptable; nothing outside that ever gets auto-accepted.
- Price is explicitly NOT a negotiation constraint. If mentioned by the
  business it is only ever captured for the narrative summary / key
  fields, never used to accept/reject a slot.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
from typing import Any, Optional


class NegotiationOutcome(str, enum.Enum):
    """Structured outcome taxonomy — mirrors Carecaller's outcome= contract.

    Drives dashboard status badges / filtering. Keep this list in sync
    with normalizer.py's OUTCOME_LABELS and extractor.py's prompt.
    """

    BOOKED = "booked"
    PENDING_USER_APPROVAL = "pending_user_approval"
    NO_AVAILABILITY = "no_availability"
    DECLINED = "declined"
    WRONG_NUMBER = "wrong_number"
    VOICEMAIL = "voicemail"
    ESCALATE_TO_HUMAN = "escalate_to_human"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


@dataclasses.dataclass
class SlotWindow:
    """A single calendar window, already known to fit the required duration.

    Precomputed by the calendar layer. Naive local datetimes are fine as
    long as both sides agree on a timezone; ISO8601 strings recommended
    for serialization.
    """

    start: dt.datetime
    end: dt.datetime

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "SlotWindow":
        return cls(
            start=dt.datetime.fromisoformat(data["start"]),
            end=dt.datetime.fromisoformat(data["end"]),
        )


@dataclasses.dataclass
class BookingRequest:
    """Inbound payload: everything voxlayer needs to run (or resume) a call.

    `booking_id` is the join key used everywhere downstream (negotiation
    state, result records, recordings, transcripts).
    """

    booking_id: str
    business_name: str
    business_phone: str  # E.164
    target_type: str  # "doctor" | "salon" | "mechanic" | ... (free text, used in prompt)
    max_date: dt.date
    required_duration_minutes: int
    fitting_slots: list[SlotWindow]
    special_instructions: str = ""
    user_display_name: str = "the client"
    call_from_number: str = ""  # optional override of default Twilio number
    is_followup_call: bool = False  # True for the "I confirmed it" callback
    prior_offer_summary: str = ""  # filled by negotiation_state when resuming

    def to_json(self) -> str:
        payload = dataclasses.asdict(self)
        payload["max_date"] = self.max_date.isoformat()
        payload["fitting_slots"] = [s.to_dict() for s in self.fitting_slots]
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "BookingRequest":
        data = json.loads(raw)
        data["max_date"] = dt.date.fromisoformat(data["max_date"])
        data["fitting_slots"] = [SlotWindow.from_dict(s) for s in data.get("fitting_slots", [])]
        return cls(**data)


@dataclasses.dataclass
class CallResult:
    """Outbound record: what voxlayer hands back after a call resolves.

    Three layers, matching the agreed design:
      1. `outcome`      -> structured status, drives UI badges/filters
      2. key fields     -> confirmed_start/end, price_mentioned, held_offer, etc.
      3. `summary`      -> LLM narrative paragraph for the user to read
    """

    booking_id: str
    call_sid: str
    outcome: NegotiationOutcome
    confirmed_start: Optional[dt.datetime]
    confirmed_end: Optional[dt.datetime]
    price_mentioned: str
    held_offer_start: Optional[dt.datetime]
    held_offer_end: Optional[dt.datetime]
    business_conditions: str  # e.g. "bring insurance card"
    summary: str
    transcript_text: str
    recording_path: str
    call_duration_seconds: int
    started_at: dt.datetime
    raw_extra: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        def _iso(value: Optional[dt.datetime]) -> Optional[str]:
            return value.isoformat() if value else None

        return {
            "booking_id": self.booking_id,
            "call_sid": self.call_sid,
            "outcome": self.outcome.value,
            "confirmed_start": _iso(self.confirmed_start),
            "confirmed_end": _iso(self.confirmed_end),
            "price_mentioned": self.price_mentioned,
            "held_offer_start": _iso(self.held_offer_start),
            "held_offer_end": _iso(self.held_offer_end),
            "business_conditions": self.business_conditions,
            "summary": self.summary,
            "transcript_text": self.transcript_text,
            "recording_path": self.recording_path,
            "call_duration_seconds": self.call_duration_seconds,
            "started_at": self.started_at.isoformat(),
            "raw_extra": self.raw_extra,
        }
