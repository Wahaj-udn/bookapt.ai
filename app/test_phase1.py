#!/usr/bin/env python3
"""Phase 1 validation script — tests the core integration without live calls.

This script:
  1. Initializes the database
  2. Creates a test business record
  3. Creates a test booking with hardcoded slots
  4. Uses booking_loader to reconstruct the BookingRequest
  5. Validates the round-trip works correctly

Run this to verify Phase 1 is complete before moving to Phase 2.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# Load environment
from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env", override=True)

import uuid
from app import db
from app.booking_loader import booking_loader


def test_phase1():
    """Run Phase 1 validation tests."""
    print()
    print("=" * 60)
    print("         Phase 1 Validation Test Suite")
    print("=" * 60)
    print()

    # Test 1: Database initialization
    print("Test 1: Database initialization...")
    try:
        db.init_db()
        print("  [OK] Database initialized successfully")
    except Exception as e:
        print(f"  [FAIL] Database initialization failed: {e}")
        return False

    # Test 2: Create business record
    print("\nTest 2: Create business record...")
    try:
        record = db.create_business_record(
            title="Dr. Smith - Apollo Clinic",
            target_type="doctor",
            phone="+11234567890",
        )
        print(f"  [OK] Created business record ID: {record['id']}")
        print(f"    Title: {record['title']}")
        print(f"    Type: {record['target_type']}")
        print(f"    Phone: {record['phone']}")
    except Exception as e:
        print(f"  [FAIL] Business record creation failed: {e}")
        return False

    # Test 3: Create booking
    print("\nTest 3: Create booking...")
    try:
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

        booking = db.create_booking(
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
        print(f"  [OK] Created booking ID: {booking_id}")
        print(f"    Status: {booking['status']}")
        print(f"    Required duration: {booking['required_duration_min']} min")
    except Exception as e:
        print(f"  [FAIL] Booking creation failed: {e}")
        return False

    # Test 4: Reconstruct BookingRequest
    print("\nTest 4: Reconstruct BookingRequest via booking_loader...")
    try:
        booking_request = booking_loader(booking_id)
        print(f"  [OK] Successfully reconstructed BookingRequest")
        print(f"    Booking ID: {booking_request.booking_id}")
        print(f"    Business: {booking_request.business_name}")
        print(f"    Phone: {booking_request.business_phone}")
        print(f"    Type: {booking_request.target_type}")
        print(f"    Max date: {booking_request.max_date}")
        print(f"    Duration: {booking_request.required_duration_minutes} min")
        print(f"    Fitting slots: {len(booking_request.fitting_slots)} slots")
        print(f"    Special instructions: {booking_request.special_instructions[:50]}...")
    except Exception as e:
        print(f"  [FAIL] BookingRequest reconstruction failed: {e}")
        return False

    # Test 5: Validate fitting_slots deserialization
    print("\nTest 5: Validate fitting_slots deserialization...")
    try:
        assert len(booking_request.fitting_slots) == 2, "Should have 2 slots"
        slot1 = booking_request.fitting_slots[0]
        print(f"  [OK] Slot 1: {slot1.start.isoformat()} -> {slot1.end.isoformat()}")
        slot2 = booking_request.fitting_slots[1]
        print(f"  [OK] Slot 2: {slot2.start.isoformat()} -> {slot2.end.isoformat()}")
    except Exception as e:
        print(f"  [FAIL] Slot validation failed: {e}")
        return False

    # Test 6: JSON serialization round-trip
    print("\nTest 6: JSON serialization round-trip...")
    try:
        json_str = booking_request.to_json()
        print(f"  [OK] Serialized to JSON ({len(json_str)} bytes)")

        from bookapt.models import BookingRequest as BR
        reconstructed = BR.from_json(json_str)
        print(f"  [OK] Deserialized back to BookingRequest")
        assert reconstructed.booking_id == booking_request.booking_id
        assert len(reconstructed.fitting_slots) == len(booking_request.fitting_slots)
        print(f"  [OK] Round-trip validation passed")
    except Exception as e:
        print(f"  [FAIL] JSON round-trip failed: {e}")
        return False

    # All tests passed
    print()
    print("=" * 60)
    print("              ALL TESTS PASSED")
    print("=" * 60)
    print()
    print("Phase 1 is complete! Next steps:")
    print("  1. Copy app/.env.example to app/.env and fill in credentials")
    print("  2. Start Flask: python app/run.py")
    print("  3. Start bridge: python app/run_bridge.py")
    print("  4. Move to Phase 2: Google Calendar integration")
    print()

    return True


if __name__ == "__main__":
    success = test_phase1()
    sys.exit(0 if success else 1)
