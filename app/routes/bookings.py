#!/usr/bin/env python3
"""Booking routes — create bookings and place calls.

This is where user data from forms gets turned into actual BookingRequests
and calls get placed through voxlayer.

Flow:
  1. User fills book.html form (preferred times, reason, calendar options)
  2. POST /bookings/create → validate + create booking in DB
  3. Compute fitting_slots (from calendar or manual)
  4. Place call via voxlayer
  5. Redirect to /bookings/<id>/call (live call page)
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from datetime import datetime, date, timedelta
import uuid

try:
    from .. import db, calendar_reader
    from ..booking_loader import booking_loader
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from app import db, calendar_reader
    from app.booking_loader import booking_loader

from bookapt.models import SlotWindow, BookingRequest

bookings_bp = Blueprint("bookings", __name__, url_prefix="/bookings")


@bookings_bp.route("/new")
def new_booking_form():
    """Show the booking form — can be pre-filled with provider_id."""
    provider_id = request.args.get("provider_id", type=int)
    provider = None

    if provider_id:
        provider = db.get_business_record(provider_id)

    # Get all providers for dropdown
    all_providers = db.get_all_business_records()

    return render_template(
        "book.html",
        provider=provider,
        all_providers=all_providers,
    )


@bookings_bp.route("/create", methods=["POST"])
def create_booking():
    """
    Create booking from form data and place call.

    Form fields expected:
      - provider_id: int (existing provider)
      - preferred_date: str (e.g. "2026-09-12")
      - preferred_time: str (e.g. "10:45 AM")
      - secondary_time: str (optional)
      - reason: str (special instructions)
      - use_calendar: checkbox (on/off)
      - calendar_window: str ("today", "tomorrow", "this_week")
      - duration: int (minutes)
    """

    # Get form data
    provider_id = request.form.get("provider_id", type=int)
    preferred_date = request.form.get("preferred_date", "").strip()
    preferred_time = request.form.get("preferred_time", "").strip()
    secondary_time = request.form.get("secondary_time", "").strip()
    reason = request.form.get("reason", "").strip()
    use_calendar = request.form.get("use_calendar") == "on"
    calendar_window = request.form.get("calendar_window", "this_week")
    duration = request.form.get("duration", 30, type=int)

    # Validation
    if not provider_id:
        flash("Please select a provider", "error")
        return redirect(url_for("bookings.new_booking_form"))

    provider = db.get_business_record(provider_id)
    if not provider:
        flash("Provider not found", "error")
        return redirect(url_for("bookings.new_booking_form"))

    if not preferred_date or not preferred_time:
        flash("Please provide at least a preferred date and time", "error")
        return redirect(url_for("bookings.new_booking_form"))

    # Parse dates/times
    try:
        # Parse preferred slot
        pref_datetime = _parse_datetime(preferred_date, preferred_time)
        pref_start = pref_datetime
        pref_end = pref_datetime + timedelta(minutes=duration)

        # Parse secondary slot if provided
        sec_start = None
        sec_end = None
        if secondary_time:
            sec_datetime = _parse_datetime(preferred_date, secondary_time)
            sec_start = sec_datetime
            sec_end = sec_datetime + timedelta(minutes=duration)

        # Determine max_date based on calendar window
        if calendar_window == "today":
            max_date = date.today()
        elif calendar_window == "tomorrow":
            max_date = date.today() + timedelta(days=1)
        else:  # "this_week"
            max_date = date.today() + timedelta(days=7)

    except ValueError as e:
        flash(f"Invalid date/time format: {e}", "error")
        return redirect(url_for("bookings.new_booking_form"))

    # Compute fitting_slots
    if use_calendar:
        # Get fitting slots from Google Calendar
        try:
            fitting_slots = calendar_reader.get_fitting_slots(
                required_duration_minutes=duration,
                max_date=max_date,
                look_ahead_days=7,
                business_hours_only=True,
            )

            if not fitting_slots:
                flash("⚠ No available slots found in your calendar. Using manual slots.", "warning")
                # Fall back to manual slots
                fitting_slots = _build_manual_slots(pref_start, pref_end, sec_start, sec_end)
        except RuntimeError as e:
            flash(f"Calendar error: {e}. Using manual slots.", "warning")
            fitting_slots = _build_manual_slots(pref_start, pref_end, sec_start, sec_end)
    else:
        # Use manual slots from form
        fitting_slots = _build_manual_slots(pref_start, pref_end, sec_start, sec_end)

    # Create booking in database
    booking_id = str(uuid.uuid4())

    try:
        booking = db.create_booking(
            booking_id=booking_id,
            business_record_id=provider_id,
            max_date=max_date.isoformat(),
            required_duration_min=duration,
            fitting_slots=[s.to_dict() for s in fitting_slots],
            preferred_slot_start=pref_start.isoformat() if pref_start else None,
            preferred_slot_end=pref_end.isoformat() if pref_end else None,
            secondary_slot_start=sec_start.isoformat() if sec_start else None,
            secondary_slot_end=sec_end.isoformat() if sec_end else None,
            use_calendar=use_calendar,
            special_instructions=reason,
        )

        flash(f"✓ Booking created! Preparing to call {provider['title']}", "success")

        # Store booking ID in session for call page
        session["current_booking_id"] = booking_id

        # Redirect to call placement page
        return redirect(url_for("bookings.place_call", booking_id=booking_id))

    except Exception as e:
        flash(f"Error creating booking: {e}", "error")
        return redirect(url_for("bookings.new_booking_form"))


@bookings_bp.route("/<booking_id>/call")
def place_call(booking_id):
    """
    Show call confirmation page and place the actual call.

    This page shows:
      - Booking summary
      - "Start AI call" button
      - After call starts, redirect to live call page
    """
    booking = db.get_booking(booking_id)
    if not booking:
        flash("Booking not found", "error")
        return redirect(url_for("dashboard.index"))

    return render_template("call.html", booking=booking, booking_id=booking_id)


@bookings_bp.route("/<booking_id>/start-call", methods=["POST"])
def start_call(booking_id):
    """
    Actually place the call through Twilio + voxlayer.

    This is the action triggered by "Start AI call" button.
    """
    try:
        # Load booking and create BookingRequest
        booking_request = booking_loader(booking_id)

        # Place call through voxlayer
        from bookapt import caller
        call_sid = caller.place_call(booking_request)

        # Update booking status
        db.update_booking_status(booking_id, "in_call", call_sid)

        flash(f"✓ Call initiated! Call SID: {call_sid}", "success")

        # Redirect to live call monitoring page
        return redirect(url_for("bookings.live_call", booking_id=booking_id))

    except Exception as e:
        flash(f"Error placing call: {e}", "error")
        return redirect(url_for("bookings.place_call", booking_id=booking_id))


@bookings_bp.route("/<booking_id>/live")
def live_call(booking_id):
    """
    Live call monitoring page — shows transcript as it updates.

    Uses AJAX polling or WebSocket to fetch live transcript.
    """
    booking = db.get_booking(booking_id)
    if not booking:
        flash("Booking not found", "error")
        return redirect(url_for("dashboard.index"))

    # TODO: Fetch live call status from voxlayer
    # For now, just show the call page
    return render_template("call.html", booking=booking, booking_id=booking_id, live=True)


@bookings_bp.route("/<booking_id>")
def view_booking(booking_id):
    """View booking details and call results."""
    booking = db.get_booking(booking_id)
    if not booking:
        flash("Booking not found", "error")
        return redirect(url_for("dashboard.index"))

    # Load call results from result_store
    call_history = {"calls": []}
    try:
        from bookapt import result_store
        call_history = result_store.get_booking_history(booking_id)
    except Exception:
        pass

    return render_template(
        "appointments.html",
        booking=booking,
        booking_id=booking_id,
        call_history=call_history,
    )


# Helper functions

def _parse_datetime(date_str: str, time_str: str) -> datetime:
    """
    Parse user-entered date and time into datetime object.

    Examples:
      date_str: "2026-09-12", "tomorrow", "today"
      time_str: "10:45 AM", "13:00", "1:00 PM"
    """
    # Handle relative dates
    if date_str.lower() == "today":
        base_date = date.today()
    elif date_str.lower() == "tomorrow":
        base_date = date.today() + timedelta(days=1)
    else:
        # Parse ISO date
        base_date = date.fromisoformat(date_str)

    # Parse time (handle both 12-hour and 24-hour formats)
    time_str = time_str.strip().upper()

    # Try 12-hour format first (10:45 AM)
    for fmt in ["%I:%M %p", "%I:%M%p", "%H:%M"]:
        try:
            time_obj = datetime.strptime(time_str, fmt).time()
            return datetime.combine(base_date, time_obj)
        except ValueError:
            continue

    raise ValueError(f"Could not parse time: {time_str}")


def _build_manual_slots(pref_start, pref_end, sec_start=None, sec_end=None) -> list[SlotWindow]:
    """Build fitting_slots from manually entered times."""
    slots = []

    if pref_start and pref_end:
        slots.append(SlotWindow(start=pref_start, end=pref_end))

    if sec_start and sec_end:
        slots.append(SlotWindow(start=sec_start, end=sec_end))

    return slots
