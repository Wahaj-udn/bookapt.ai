#!/usr/bin/env python3
"""Booking loader — the seam between the host app and voxlayer.

This is the callable that the bridge process expects. It reconstructs a
`BookingRequest` from the database on every call (initial or followup).

Why this exists
---------------
The bridge (run_bridge.py) is a separate asyncio process. It doesn't import
the Flask app, doesn't know the DB schema, and doesn't care how bookings are
stored. It just needs a function that takes `booking_id` and returns a
`BookingRequest`.

This module is that adapter.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from bookapt.models import BookingRequest, SlotWindow

# Import from the same package (app.db)
try:
    from . import db
except ImportError:
    # Allow running as script for testing
    import db


def booking_loader(booking_id: str) -> BookingRequest:
    """Load a booking from the database and reconstruct a BookingRequest.

    Called by the bridge process on every call (initial or followup).

    Raises:
        ValueError: If booking_id doesn't exist in the database.
    """
    row = db.get_booking(booking_id)
    if row is None:
        raise ValueError(f"Booking {booking_id} not found in database")

    # Deserialize fitting_slots from JSON
    fitting_slots_data = json.loads(row.get("fitting_slots_json", "[]"))
    fitting_slots = [SlotWindow.from_dict(s) for s in fitting_slots_data]

    # Parse max_date (stored as ISO string)
    max_date = dt.date.fromisoformat(row["max_date"])

    # Reconstruct the BookingRequest
    # Note: is_followup_call and prior_offer_summary are managed by
    # negotiation_state.py, not stored in the DB. The caller (run_bridge.py
    # or the /alerts approval route) will override these if needed.
    return BookingRequest(
        booking_id=row["id"],
        business_name=row["title"],  # from joined business_records.title
        business_phone=row["phone"],  # from joined business_records.phone
        target_type=row["target_type"],  # from joined business_records.target_type
        max_date=max_date,
        required_duration_minutes=row["required_duration_min"],
        fitting_slots=fitting_slots,
        special_instructions=row.get("special_instructions", ""),
        user_display_name="the client",  # TODO: Pull from user profile in Phase 9
        call_from_number="",  # Uses default Twilio number from config
        is_followup_call=False,  # Caller overrides if needed
        prior_offer_summary="",  # Caller overrides if needed
    )


def test_loader() -> None:
    """Minimal smoke test — prints a reconstructed booking as JSON."""
    import sys
    from pathlib import Path

    # Add parent directory to path so we can import bookapt
    sys.path.insert(0, str(Path(__file__).parent.parent))

    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")

    db.init_db()

    # Create a test business record
    record = db.create_business_record(
        title="Dr. Smith — Apollo Clinic",
        target_type="doctor",
        phone="+11234567890",
    )

    # Create a test booking with hardcoded slots
    import uuid
    booking_id = str(uuid.uuid4())

    fitting_slots = [
        {
            "start": "2026-09-12T10:00:00",
            "end": "2026-09-12T12:00:00",
        },
        {
            "start": "2026-09-13T14:00:00",
            "end": "2026-09-13T17:00:00",
        },
    ]

    db.create_booking(
        booking_id=booking_id,
        business_record_id=record["id"],
        max_date="2026-09-20",
        required_duration_min=30,
        fitting_slots=fitting_slots,
        preferred_slot_start="2026-09-12T10:00:00",
        preferred_slot_end="2026-09-12T10:30:00",
        use_calendar=False,
        special_instructions="I need a follow-up for blood work results.",
    )

    print(f"✓ Created test booking: {booking_id}")
    print()

    # Test the loader
    booking_request = booking_loader(booking_id)
    print("✓ Reconstructed BookingRequest:")
    print(booking_request.to_json())


if __name__ == "__main__":
    test_loader()
