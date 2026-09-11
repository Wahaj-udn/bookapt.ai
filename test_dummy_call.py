#!/usr/bin/env python3
"""Test script — Create a dummy booking and test the voxlayer calling flow.

This creates a realistic BookingRequest with hardcoded slots (no calendar needed)
and places a test call through Twilio + Gemini bridge.

Prerequisites:
  - Twilio credentials in bookapt/.env
  - Gemini API key in bookapt/.env
  - Bridge running: python app/run_bridge.py
  - Webhook server running: python app/run.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, date, timedelta
import uuid

# Add project root to path
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env")

from app import db
from bookapt.models import BookingRequest, SlotWindow
from bookapt import caller


def create_dummy_booking() -> str:
    """Create a test booking in the database with hardcoded slots."""

    print()
    print("=" * 70)
    print("  Creating Dummy Booking for Call Test")
    print("=" * 70)
    print()

    # Initialize database
    db.init_db()

    # Create or get test business record
    business = db.create_business_record(
        title="Dr. Sarah Smith - Apollo Clinic",
        target_type="doctor",
        phone="+1234567890",  # Replace with a real test number
    )

    print(f"[1] Business Record: {business['title']}")
    print(f"    Phone: {business['phone']}")
    print()

    # Create hardcoded time slots (next 3 days, business hours)
    today = datetime.now()

    # Tomorrow 10am-12pm
    slot1_start = (today + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    slot1_end = slot1_start.replace(hour=12)

    # Day after tomorrow 2pm-5pm
    slot2_start = (today + timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0)
    slot2_end = slot2_start.replace(hour=17)

    # Three days from now 9am-11am
    slot3_start = (today + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
    slot3_end = slot3_start.replace(hour=11)

    fitting_slots = [
        {"start": slot1_start.isoformat(), "end": slot1_end.isoformat()},
        {"start": slot2_start.isoformat(), "end": slot2_end.isoformat()},
        {"start": slot3_start.isoformat(), "end": slot3_end.isoformat()},
    ]

    print("[2] Available Time Slots:")
    for i, slot in enumerate(fitting_slots, 1):
        start = datetime.fromisoformat(slot["start"])
        end = datetime.fromisoformat(slot["end"])
        print(f"    Slot {i}: {start.strftime('%a %b %d, %I:%M %p')} - {end.strftime('%I:%M %p')}")
    print()

    # Max date: 7 days from now
    max_date = (today + timedelta(days=7)).date()

    # Create booking
    booking_id = str(uuid.uuid4())

    booking = db.create_booking(
        booking_id=booking_id,
        business_record_id=business["id"],
        max_date=max_date.isoformat(),
        required_duration_min=30,
        fitting_slots=fitting_slots,
        preferred_slot_start=slot1_start.isoformat(),
        preferred_slot_end=(slot1_start + timedelta(minutes=30)).isoformat(),
        use_calendar=False,  # Using hardcoded slots, not calendar
        special_instructions="This is a follow-up appointment for blood work results. Please mention patient ID: TEST-12345.",
    )

    print(f"[3] Booking Created:")
    print(f"    ID: {booking_id}")
    print(f"    Status: {booking['status']}")
    print(f"    Required Duration: {booking['required_duration_min']} minutes")
    print(f"    Max Date: {max_date.isoformat()}")
    print(f"    Special Instructions: {booking['special_instructions'][:60]}...")
    print()

    return booking_id


def test_call_placement(booking_id: str, dry_run: bool = True):
    """Test placing a call through voxlayer."""

    print("=" * 70)
    print("  Test Call Placement")
    print("=" * 70)
    print()

    # Load booking and create BookingRequest
    from app.booking_loader import booking_loader

    try:
        booking_request = booking_loader(booking_id)
    except Exception as e:
        print(f"[ERROR] Failed to load booking: {e}")
        return

    print("[4] BookingRequest Created:")
    print(f"    Booking ID: {booking_request.booking_id}")
    print(f"    Business: {booking_request.business_name}")
    print(f"    Phone: {booking_request.business_phone}")
    print(f"    Target Type: {booking_request.target_type}")
    print(f"    Fitting Slots: {len(booking_request.fitting_slots)}")
    print(f"    Max Date: {booking_request.max_date}")
    print()

    if dry_run:
        print("[DRY RUN] Not placing actual call.")
        print()
        print("What would happen:")
        print("  1. caller.place_call() would be invoked")
        print("  2. Twilio would initiate outbound call")
        print("  3. Bridge would connect and start Gemini negotiation")
        print("  4. Agent would propose times, check against fitting_slots")
        print("  5. Result would be saved to results/<booking_id>.json")
        print()
        print("To place a REAL call:")
        print("  1. Make sure Twilio credentials are in bookapt/.env")
        print("  2. Make sure Gemini API key is in bookapt/.env")
        print("  3. Start bridge: python app/run_bridge.py")
        print("  4. Start webhook server: python app/run.py")
        print("  5. Run this script with: python test_dummy_call.py --real")
        print()
    else:
        print("[LIVE] Placing real call through Twilio...")
        print()

        try:
            call_sid = caller.place_call(booking_request)
            print(f"[SUCCESS] Call initiated!")
            print(f"    Call SID: {call_sid}")
            print(f"    Status: Check voxlayer logs for progress")
            print()
            print("Monitor the call:")
            print(f"  - Bridge logs: python app/run_bridge.py output")
            print(f"  - Webhook logs: python app/run.py output")
            print(f"  - Result file: bookapt_data/results/{booking_id}.json")
            print()
        except Exception as e:
            print(f"[ERROR] Call placement failed: {e}")
            print()
            print("Common issues:")
            print("  - Twilio credentials not set")
            print("  - Bridge not running")
            print("  - Webhook server not running")
            print("  - Invalid phone number")
            print()


def show_booking_payload(booking_id: str):
    """Show the complete JSON payload that will be sent."""

    print("=" * 70)
    print("  Full BookingRequest Payload (JSON)")
    print("=" * 70)
    print()

    from app.booking_loader import booking_loader

    try:
        booking_request = booking_loader(booking_id)
        print(booking_request.to_json())
        print()
    except Exception as e:
        print(f"[ERROR] Failed to generate payload: {e}")


if __name__ == "__main__":
    import sys

    # Check if --real flag is passed
    real_call = "--real" in sys.argv

    print()
    print("=" * 70)
    print("  DUMMY BOOKING & CALL TEST")
    print("=" * 70)
    print()

    # Step 1: Create dummy booking
    booking_id = create_dummy_booking()

    # Step 2: Show full payload
    show_booking_payload(booking_id)

    # Step 3: Test call placement (dry run by default)
    test_call_placement(booking_id, dry_run=not real_call)

    print("=" * 70)
    print("  Test Complete")
    print("=" * 70)
    print()

    if not real_call:
        print("Booking created and ready. To place a real call:")
        print(f"  python test_dummy_call.py --real")
        print()

    print(f"Booking ID: {booking_id}")
    print()
