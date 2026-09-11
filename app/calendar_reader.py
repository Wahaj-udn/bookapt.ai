#!/usr/bin/env python3
"""Google Calendar integration — credential storage and free/busy slot finder.

This module provides:
  - Persistent credential storage (file-based, not session)
  - Automatic token refresh
  - Free/busy slot detection for booking negotiations

Used by:
  - routes/calendar_auth.py (OAuth flow)
  - routes/bookings.py (when creating bookings with calendar integration)
"""

from __future__ import annotations

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from bookapt.models import SlotWindow

try:
    from . import config
except ImportError:
    import config


# --------------------------------------------------------------------------
# Credential persistence (file-based, not session)
# --------------------------------------------------------------------------

def creds_to_dict(creds: Credentials) -> dict:
    """Convert Credentials object to dict for JSON serialization."""
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }


def save_credentials(creds: Credentials) -> None:
    """Save credentials to the configured token file."""
    token_path = Path(config.Config.GOOGLE_TOKEN_PATH)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(creds_to_dict(creds), indent=2))


def load_credentials() -> Optional[Credentials]:
    """
    Load credentials from the token file, refreshing if expired.

    Returns:
        Credentials object if valid token exists, None otherwise.
    """
    token_path = Path(config.Config.GOOGLE_TOKEN_PATH)

    if not token_path.exists():
        return None

    try:
        creds_data = json.loads(token_path.read_text())
        creds = Credentials(**creds_data)
    except (json.JSONDecodeError, KeyError, TypeError):
        # Corrupted token file
        return None

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            save_credentials(creds)  # Persist the new access token
        except Exception:
            # Refresh failed — user needs to re-authenticate
            return None

    return creds


def clear_credentials() -> None:
    """Delete the stored token file (for logout)."""
    token_path = Path(config.Config.GOOGLE_TOKEN_PATH)
    if token_path.exists():
        token_path.unlink()


# --------------------------------------------------------------------------
# Free/busy slot finder (the core feature for BookAppt.ai)
# --------------------------------------------------------------------------

def get_fitting_slots(
    required_duration_minutes: int,
    max_date: date,
    look_ahead_days: int = 14,
    business_hours_only: bool = True,
    start_hour: int = 9,
    end_hour: int = 17,
) -> list[SlotWindow]:
    """
    Find free time windows in Google Calendar that fit the required duration.

    Uses the freebusy API to check availability without reading event details.
    This is privacy-friendly (doesn't expose event titles/details) and fast.

    Args:
        required_duration_minutes: Minimum gap size to consider (e.g. 30 for a 30-min appointment)
        max_date: Hard ceiling — no slots after this date
        look_ahead_days: How many days forward to search (default 14)
        business_hours_only: If True, only return slots within start_hour—end_hour
        start_hour: Business day start (default 9am)
        end_hour: Business day end (default 5pm)

    Returns:
        List of SlotWindow objects representing available time ranges.
        Empty list if no credentials or no availability.

    Raises:
        RuntimeError: If credentials are missing or API call fails.
    """
    creds = load_credentials()
    if not creds:
        raise RuntimeError(
            "No Google Calendar credentials found. User must authenticate first via /calendar/auth"
        )

    service = build("calendar", "v3", credentials=creds)

    # Define search window
    now = datetime.now()
    search_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    search_end = min(
        search_start + timedelta(days=look_ahead_days),
        datetime.combine(max_date, datetime.max.time())
    )

    # Query free/busy for primary calendar
    body = {
        "timeMin": search_start.isoformat() + "Z",
        "timeMax": search_end.isoformat() + "Z",
        "items": [{"id": "primary"}],
    }

    try:
        freebusy_result = service.freebusy().query(body=body).execute()
    except Exception as e:
        raise RuntimeError(f"Google Calendar API error: {e}")

    busy_blocks = freebusy_result.get("calendars", {}).get("primary", {}).get("busy", [])

    # Convert busy blocks to datetime objects (remove 'Z' suffix and parse as naive UTC)
    busy_times = []
    for block in busy_blocks:
        start_str = block["start"].replace("Z", "")
        end_str = block["end"].replace("Z", "")
        busy_times.append((
            datetime.fromisoformat(start_str),
            datetime.fromisoformat(end_str)
        ))

    # Sort busy blocks by start time
    busy_times.sort(key=lambda x: x[0])

    # Find gaps between busy blocks that are >= required_duration_minutes
    fitting_slots = []
    current_time = max(now, search_start)  # Don't suggest slots in the past

    for busy_start, busy_end in busy_times:
        # Calculate gap before this busy block
        gap_start = current_time
        gap_end = busy_start

        # Apply business hours filter if enabled
        if business_hours_only:
            gap_start, gap_end = _clamp_to_business_hours(
                gap_start, gap_end, start_hour, end_hour
            )

        if gap_start < gap_end:
            gap_duration_minutes = (gap_end - gap_start).total_seconds() / 60

            if gap_duration_minutes >= required_duration_minutes:
                fitting_slots.append(SlotWindow(start=gap_start, end=gap_end))

        current_time = max(current_time, busy_end)

    # Check final gap after last busy block
    final_gap_start = current_time
    final_gap_end = search_end

    if business_hours_only:
        final_gap_start, final_gap_end = _clamp_to_business_hours(
            final_gap_start, final_gap_end, start_hour, end_hour
        )

    if final_gap_start < final_gap_end:
        final_gap_duration = (final_gap_end - final_gap_start).total_seconds() / 60

        if final_gap_duration >= required_duration_minutes:
            fitting_slots.append(SlotWindow(start=final_gap_start, end=final_gap_end))

    return fitting_slots


def _clamp_to_business_hours(
    start: datetime,
    end: datetime,
    start_hour: int,
    end_hour: int
) -> tuple[datetime, datetime]:
    """
    Clamp a time window to business hours (e.g. 9am–5pm).

    If the window spans multiple days, this only clamps the first day.
    Multi-day logic would require splitting into per-day windows (future enhancement).
    """
    business_start = start.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    business_end = start.replace(hour=end_hour, minute=0, second=0, microsecond=0)

    clamped_start = max(start, business_start)
    clamped_end = min(end, business_end)

    return clamped_start, clamped_end


# --------------------------------------------------------------------------
# Helper: Get user info (optional, for display purposes)
# --------------------------------------------------------------------------

def get_user_info() -> Optional[dict]:
    """
    Fetch basic user info (email, name) from Google.

    Returns:
        {"email": "...", "name": "..."} if credentials exist, None otherwise.
    """
    creds = load_credentials()
    if not creds:
        return None

    try:
        oauth2_service = build("oauth2", "v2", credentials=creds)
        user_info = oauth2_service.userinfo().get().execute()
        return {
            "email": user_info.get("email"),
            "name": user_info.get("name"),
        }
    except Exception:
        return None
