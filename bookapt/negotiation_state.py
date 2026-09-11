#!/usr/bin/env python3
"""Per-booking negotiation state machine.

Carecaller never needed this — one call, one result. BookAppt.ai's
negotiation flow can span two calls with a human-approval gap in between:

    NEW
     -> IN_CALL                  (first call placed)
     -> BOOKED                   (agent found + confirmed a fitting slot solo)
     -> NO_AVAILABILITY          (business had nothing that could ever fit)
     -> HELD_PENDING_APPROVAL    (business offered something outside auto-accept
                                   rules; agent asked them to hold, disconnected,
                                   and is waiting on the user)
     -> IN_CALL (followup)       (user approved; callback placed, references
                                   the prior held offer)
     -> BOOKED / NO_AVAILABILITY (resolved by the followup call)

There is deliberately no timeout on HELD_PENDING_APPROVAL: per spec, the
followup call simply never fires until the user explicitly approves.

State is persisted as one JSON file per booking_id so this module has no
database dependency and can be swapped for a real DB later without
touching callers.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .models import NegotiationOutcome


class NegotiationState:
    NEW = "new"
    IN_CALL = "in_call"
    HELD_PENDING_APPROVAL = "held_pending_approval"
    BOOKED = "booked"
    NO_AVAILABILITY = "no_availability"
    DECLINED = "declined"
    WRONG_NUMBER = "wrong_number"
    VOICEMAIL = "voicemail"
    ESCALATE_TO_HUMAN = "escalate_to_human"

    TERMINAL_STATES = {BOOKED, NO_AVAILABILITY, DECLINED, WRONG_NUMBER, VOICEMAIL, ESCALATE_TO_HUMAN}


def _state_dir() -> Path:
    path = Path(Config.data_path(Config.NEGOTIATION_STATE_DIR, "negotiation_state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_file(booking_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in booking_id)
    return _state_dir() / f"{safe_id}.json"


@dataclasses.dataclass
class NegotiationRecord:
    booking_id: str
    state: str = NegotiationState.NEW
    call_count: int = 0
    call_sids: list[str] = dataclasses.field(default_factory=list)
    held_offer_start: Optional[str] = None  # ISO strings for JSON-friendliness
    held_offer_end: Optional[str] = None
    last_outcome: Optional[str] = None
    updated_at: str = dataclasses.field(default_factory=lambda: dt.datetime.utcnow().isoformat())
    history: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NegotiationRecord":
        return cls(**data)


def load(booking_id: str) -> NegotiationRecord:
    path = _state_file(booking_id)
    if not path.exists():
        return NegotiationRecord(booking_id=booking_id)
    try:
        return NegotiationRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        # Corrupt state file: start fresh rather than crash the pipeline.
        return NegotiationRecord(booking_id=booking_id)


def save(record: NegotiationRecord) -> None:
    record.updated_at = dt.datetime.utcnow().isoformat()
    path = _state_file(record.booking_id)
    path.write_text(json.dumps(record.to_dict(), indent=2) + "\n", encoding="utf-8")


def mark_call_started(booking_id: str, call_sid: str) -> NegotiationRecord:
    record = load(booking_id)
    record.state = NegotiationState.IN_CALL
    record.call_count += 1
    record.call_sids.append(call_sid)
    record.history.append(
        {"event": "call_started", "call_sid": call_sid, "at": dt.datetime.utcnow().isoformat()}
    )
    save(record)
    return record


def mark_held_pending_approval(
    booking_id: str,
    held_start: dt.datetime,
    held_end: dt.datetime,
    reason: str = "",
) -> NegotiationRecord:
    """Called when a call ends with the business asked to hold a slot outside
    the auto-accept rules. Dashboard alert should be triggered by the host
    application when it observes this state (poll `load()` or watch the
    result record — see result_store.py)."""
    record = load(booking_id)
    record.state = NegotiationState.HELD_PENDING_APPROVAL
    record.held_offer_start = held_start.isoformat()
    record.held_offer_end = held_end.isoformat()
    record.last_outcome = NegotiationOutcome.PENDING_USER_APPROVAL.value
    record.history.append(
        {
            "event": "held_pending_approval",
            "held_start": held_start.isoformat(),
            "held_end": held_end.isoformat(),
            "reason": reason,
            "at": dt.datetime.utcnow().isoformat(),
        }
    )
    save(record)
    return record


def mark_resolved(booking_id: str, outcome: NegotiationOutcome) -> NegotiationRecord:
    record = load(booking_id)
    terminal_map = {
        NegotiationOutcome.BOOKED: NegotiationState.BOOKED,
        NegotiationOutcome.NO_AVAILABILITY: NegotiationState.NO_AVAILABILITY,
        NegotiationOutcome.DECLINED: NegotiationState.DECLINED,
        NegotiationOutcome.WRONG_NUMBER: NegotiationState.WRONG_NUMBER,
        NegotiationOutcome.VOICEMAIL: NegotiationState.VOICEMAIL,
        NegotiationOutcome.ESCALATE_TO_HUMAN: NegotiationState.ESCALATE_TO_HUMAN,
        NegotiationOutcome.PENDING_USER_APPROVAL: NegotiationState.HELD_PENDING_APPROVAL,
    }
    record.state = terminal_map.get(outcome, record.state)
    record.last_outcome = outcome.value
    record.history.append(
        {"event": "resolved", "outcome": outcome.value, "at": dt.datetime.utcnow().isoformat()}
    )
    save(record)
    return record


def approve_held_offer(booking_id: str) -> NegotiationRecord:
    """User approves the held offer from the dashboard. Does NOT place the
    call itself — caller.py's place_followup_call() should be invoked next
    by the host application, using record.held_offer_start/end as the
    slot to confirm on the callback."""
    record = load(booking_id)
    if record.state != NegotiationState.HELD_PENDING_APPROVAL:
        raise ValueError(
            f"Cannot approve booking {booking_id}: state is '{record.state}', "
            f"expected '{NegotiationState.HELD_PENDING_APPROVAL}'."
        )
    record.history.append({"event": "user_approved_hold", "at": dt.datetime.utcnow().isoformat()})
    save(record)
    return record


def build_prior_offer_summary(record: NegotiationRecord) -> str:
    """Human-readable line injected into the followup call's mission prompt,
    e.g. "You previously spoke with this business and they offered ...".
    """
    if not record.held_offer_start or not record.held_offer_end:
        return ""
    return (
        f"You have called this business before about this booking. They offered "
        f"an appointment from {record.held_offer_start} to {record.held_offer_end}, "
        f"which your client has now approved. Confirm this exact slot on this call, "
        f"and mention that you spoke with them previously about it."
    )


def is_pending_approval(booking_id: str) -> bool:
    return load(booking_id).state == NegotiationState.HELD_PENDING_APPROVAL


def list_pending_approvals() -> list[NegotiationRecord]:
    """Used by the dashboard's alerts section."""
    records = []
    for path in _state_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = NegotiationRecord.from_dict(data)
        except Exception:
            continue
        if record.state == NegotiationState.HELD_PENDING_APPROVAL:
            records.append(record)
    return records
