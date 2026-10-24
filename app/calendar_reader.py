#!/usr/bin/env python3
"""Google Calendar integration — credential storage, free/busy, event write-back.

This module provides:
  - Persistent credential storage (file-based, not session)
  - Automatic token refresh
  - Free/busy slot detection for booking negotiations
  - Creating a Calendar event when a booking is confirmed

Used by:
  - routes/calendar_auth.py (OAuth flow)
  - routes/bookings.py (when creating bookings with calendar integration)
  - bookapt/server.py post-call pipeline (write confirmed appointments)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# zoneinfo is stdlib in Python 3.9+; fall back gracefully if missing.
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo as _ZoneInfo  # type: ignore
    except ImportError:
        _ZoneInfo = None  # type: ignore

LOGGER = logging.getLogger("bookapt.calendar")

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
    expiry = creds.expiry.isoformat() if getattr(creds, "expiry", None) else None
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "expiry": expiry,
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
        expiry_raw = creds_data.pop("expiry", None)
        if isinstance(expiry_raw, str) and expiry_raw:
            creds_data["expiry"] = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
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
# Timezone helpers
# --------------------------------------------------------------------------

def _local_tz_name() -> str:
    configured = (config.Config.GOOGLE_CALENDAR_TIMEZONE or "").strip()
    if configured:
        return configured
    tz = datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    return "Asia/Kolkata"


def _get_local_tzinfo():
    """Return a tzinfo object for the user's configured local timezone.

    Used to convert UTC datetimes from the Google Calendar API into the
    same naive-local timezone that user-entered times live in.
    Falls back to the OS system timezone if zoneinfo is unavailable.
    """
    tz_name = _local_tz_name()
    if _ZoneInfo is not None:
        try:
            return _ZoneInfo(tz_name)
        except Exception:
            pass
    # Fallback: system local timezone
    return datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------
# Busy-block cache builder  (called once at booking creation)
# --------------------------------------------------------------------------

def fetch_busy_blocks_for_booking(
    booking_id: str,
    preferred_date: date,
    max_date: date,
) -> list[tuple[datetime, datetime]]:
    """Query Google Calendar and write ALL busy blocks to the busy_store JSON cache.

    This is the SINGLE entry point for building the busy cache for a booking.
    It is called ONCE when the booking form is submitted.  During the live call,
    check_slot_fits reads the saved JSON file — no further API calls are made.

    Args:
        booking_id:     UUID of the booking (used as the JSON filename key).
        preferred_date: First day to include in the query (user's preferred date).
        max_date:       Last day to include (user's deadline/limit date).

    Returns:
        List of (start, end) naive-local datetime pairs that were saved.

    Raises:
        RuntimeError: If credentials are missing or the Calendar API fails.
    """
    creds = load_credentials()
    if not creds:
        raise RuntimeError(
            "No Google Calendar credentials. Authenticate at /calendar/auth first."
        )

    service = build("calendar", "v3", credentials=creds)
    local_tz = _get_local_tzinfo()
    tz_name = _local_tz_name()

    # Query window: midnight of preferred_date → end of max_date, in LOCAL timezone.
    # Converting from local midnight (not UTC midnight) so every calendar day is
    # fully covered regardless of the user's UTC offset.
    q_start = datetime(
        preferred_date.year, preferred_date.month, preferred_date.day,
        0, 0, 0, tzinfo=local_tz,
    ).astimezone(timezone.utc)

    q_end = datetime(
        max_date.year, max_date.month, max_date.day,
        23, 59, 59, tzinfo=local_tz,
    ).astimezone(timezone.utc)

    LOGGER.info(
        "busy_cache [%s]: querying calendar %s → %s  (UTC: %s → %s)",
        booking_id, preferred_date, max_date,
        q_start.isoformat(), q_end.isoformat(),
    )

    body = {
        "timeMin": q_start.isoformat(),
        "timeMax": q_end.isoformat(),
        "items": [{"id": "primary"}],
    }

    try:
        result = service.freebusy().query(body=body).execute()
    except Exception as exc:
        raise RuntimeError(f"Google Calendar freebusy API error: {exc}")

    raw_busy = result.get("calendars", {}).get("primary", {}).get("busy", [])
    LOGGER.info(
        "busy_cache [%s]: API returned %d busy block(s): %s",
        booking_id, len(raw_busy), raw_busy,
    )

    # Convert UTC blocks to naive LOCAL datetimes for storage
    busy_local: list[tuple[datetime, datetime]] = []
    for block in raw_busy:
        b_start = datetime.fromisoformat(block["start"].replace("Z", "+00:00")).astimezone(local_tz)
        b_end   = datetime.fromisoformat(block["end"].replace("Z", "+00:00")).astimezone(local_tz)
        busy_local.append((b_start, b_end))
        LOGGER.info(
            "busy_cache [%s]:  block %s → %s  (local %s)",
            booking_id, block["start"], block["end"],
            f"{b_start.strftime('%Y-%m-%d %H:%M')} → {b_end.strftime('%H:%M')}",
        )

    busy_local.sort(key=lambda x: x[0])

    from bookapt import busy_store
    busy_store.save(booking_id, busy_local, max_date, tz_name)

    return busy_local


# --------------------------------------------------------------------------
# Free/busy slot finder (used only for mission-prompt context)
# --------------------------------------------------------------------------

def get_fitting_slots(
    required_duration_minutes: int,
    max_date: date,
    look_ahead_days: int = 14,
    business_hours_only: bool = True,
    start_hour: int = 9,
    end_hour: int = 17,
    preferred_date: Optional[date] = None,
    booking_id: Optional[str] = None,
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
        preferred_date: If provided, start the calendar search from this date (not today).
                        This ensures the agent only sees slots on/after what the user requested.
        booking_id: If provided, write the raw busy blocks to the busy_store cache so that
                    check_slot_fits can do direct overlap detection during the live call.

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

    # All gap and business-hours arithmetic must happen in the USER's local
    # timezone, not UTC.  Doing it in UTC would mean:
    #   - "9am–5pm" is applied in UTC, which is wrong for IST users (it would
    #     produce 2:30pm–10:30pm IST windows instead of 9am–5pm IST).
    #   - Busy blocks in UTC appear at the wrong local-time positions, so a
    #     9:30am IST busy block (=4am UTC) would fall outside the 9am-5pm UTC
    #     window and seem to leave 9am–5pm free.
    local_tz = _get_local_tzinfo()
    now_local = datetime.now(local_tz)

    # Build the freebusy query window in UTC (the API only speaks UTC).
    # We start from UTC midnight of preferred_date so we capture the full
    # local day even for timezones that are ahead of UTC.
    now_utc = datetime.now(timezone.utc)
    if preferred_date is not None and preferred_date > now_utc.date():
        search_start_utc = datetime(
            preferred_date.year, preferred_date.month, preferred_date.day,
            0, 0, 0, tzinfo=timezone.utc
        )
    else:
        search_start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    search_end_utc = min(
        search_start_utc + timedelta(days=look_ahead_days),
        datetime(
            max_date.year, max_date.month, max_date.day,
            23, 59, 59, tzinfo=timezone.utc
        ),
    )

    body = {
        "timeMin": search_start_utc.isoformat(),
        "timeMax": search_end_utc.isoformat(),
        "items": [{"id": "primary"}],
    }

    try:
        freebusy_result = service.freebusy().query(body=body).execute()
    except Exception as e:
        raise RuntimeError(f"Google Calendar API error: {e}")

    busy_blocks = freebusy_result.get("calendars", {}).get("primary", {}).get("busy", [])

    # Convert busy blocks and search boundaries to LOCAL timezone so that
    # business-hours filtering and gap math are in the same coordinate system
    # as user-entered times and agent proposals.
    busy_times_local = []
    for block in busy_blocks:
        b_start = datetime.fromisoformat(block["start"].replace("Z", "+00:00")).astimezone(local_tz)
        b_end = datetime.fromisoformat(block["end"].replace("Z", "+00:00")).astimezone(local_tz)
        busy_times_local.append((b_start, b_end))

    busy_times_local.sort(key=lambda x: x[0])

    # Write raw busy blocks to the busy_store cache so check_slot_fits can do
    # authoritative direct overlap detection during the live call — no gap
    # arithmetic needed, no edge-cases from business-hours filtering.
    if booking_id:
        try:
            from bookapt import busy_store
            busy_store.save(booking_id, busy_times_local, max_date, _local_tz_name())
        except Exception as exc:
            LOGGER.warning("busy_store: failed to save cache for %s: %s", booking_id, exc)

    # Convert search boundaries to local time for gap iteration.
    search_start_local = search_start_utc.astimezone(local_tz)
    search_end_local = search_end_utc.astimezone(local_tz)

    # Walk forward through the free gaps in LOCAL time, emitting per-day
    # business-hour windows as naive local datetimes.
    fitting_slots: list[SlotWindow] = []
    current_time = search_start_local

    def _add_gap(gap_start: datetime, gap_end: datetime) -> None:
        """Expand a free local-time window into per-day business-hour SlotWindows."""
        if gap_start >= gap_end:
            return
        if business_hours_only:
            for ws, we in _iter_daily_business_windows(gap_start, gap_end, start_hour, end_hour):
                if (we - ws).total_seconds() / 60 >= required_duration_minutes:
                    # Strip tzinfo: SlotWindows are stored as naive local datetimes
                    # to be consistent with user-entered preferred/secondary slots.
                    fitting_slots.append(SlotWindow(
                        start=ws.replace(tzinfo=None),
                        end=we.replace(tzinfo=None),
                    ))
        else:
            if (gap_end - gap_start).total_seconds() / 60 >= required_duration_minutes:
                fitting_slots.append(SlotWindow(
                    start=gap_start.replace(tzinfo=None),
                    end=gap_end.replace(tzinfo=None),
                ))

    for busy_start, busy_end in busy_times_local:
        _add_gap(current_time, busy_start)
        current_time = max(current_time, busy_end)

    # Final gap (or the entire window when calendar is empty)
    _add_gap(current_time, search_end_local)

    return fitting_slots


def _iter_daily_business_windows(
    gap_start: datetime,
    gap_end: datetime,
    start_hour: int,
    end_hour: int,
) -> list[tuple[datetime, datetime]]:
    """Split a potentially multi-day free gap into per-calendar-day business-hour windows.

    This is the correct replacement for the old `_clamp_to_business_hours`, which
    silently dropped every day after the first in a multi-day gap. With an empty
    Google Calendar the entire search window is one big gap, so the old helper
    would produce only a single slot for the starting day and nothing for the rest.

    Returns a list of (window_start, window_end) pairs, one per calendar day that
    has at least one minute of overlap with the gap and with business hours.
    All datetimes carry the same tzinfo as gap_start.
    """
    windows: list[tuple[datetime, datetime]] = []
    tz = gap_start.tzinfo
    current_date = gap_start.date()
    end_date = gap_end.date()

    while current_date <= end_date:
        biz_start = datetime(
            current_date.year, current_date.month, current_date.day,
            start_hour, 0, 0, tzinfo=tz,
        )
        biz_end = datetime(
            current_date.year, current_date.month, current_date.day,
            end_hour, 0, 0, tzinfo=tz,
        )
        # Clamp to the actual free gap boundaries
        day_start = max(gap_start, biz_start)
        day_end = min(gap_end, biz_end)
        if day_start < day_end:
            windows.append((day_start, day_end))
        current_date += timedelta(days=1)

    return windows


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


# --------------------------------------------------------------------------
# Write confirmed bookings back to Google Calendar
# --------------------------------------------------------------------------


def _parse_event_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string, returning a naive LOCAL datetime.

    The Google Calendar event body is built with a separate timeZone field,
    so the dateTime string must be a naive local time (no offset). Passing
    a UTC-offset string alongside a timeZone name causes Calendar to
    double-convert and land the event at the wrong hour.

    - Naive input  (extractor returned local time from transcript) → keep as-is.
    - Aware input  (e.g. UTC from another source)                  → convert to
      local timezone, then strip tzinfo.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Already a naive local datetime (typical extractor output) — return as-is.
        return parsed
    # Timezone-aware → convert to local, strip tzinfo.
    local_tz = _get_local_tzinfo()
    return parsed.astimezone(local_tz).replace(tzinfo=None)


def _build_event_description(
    *,
    business_name: str,
    business_phone: str,
    target_type: str,
    special_instructions: str,
    summary: str,
    price_mentioned: str,
    business_conditions: str,
) -> str:
    lines = [
        "Booked by BookAppt.ai",
        "",
        f"Provider: {business_name}",
    ]
    if target_type:
        lines.append(f"Type: {target_type}")
    if business_phone:
        lines.append(f"Phone: {business_phone}")
    if special_instructions:
        lines.extend(["", "What you asked for:", special_instructions])
    if business_conditions:
        lines.extend(["", "Notes from the business:", business_conditions])
    if price_mentioned:
        lines.append(f"Price mentioned: {price_mentioned}")
    if summary:
        lines.extend(["", "Call summary:", summary])
    lines.extend(["", "Created automatically after the AI booking call."])
    return "\n".join(lines)


def maybe_add_booked_event(
    booking_row: Optional[dict[str, Any]],
    extraction: dict[str, Any],
) -> Optional[str]:
    """Create a Google Calendar event when a call outcome is `booked`.

    Returns the event's htmlLink, or None if this outcome should not be written.
    Raises RuntimeError if credentials/scopes are missing or the API call fails.
    """
    if (extraction.get("outcome") or "").strip().lower() != "booked":
        return None

    start = _parse_event_datetime(extraction.get("confirmed_start_iso"))
    if start is None:
        LOGGER.info("calendar write skipped: booked outcome has no confirmed_start_iso")
        return None

    duration = 30
    if booking_row and booking_row.get("required_duration_min"):
        try:
            duration = int(booking_row["required_duration_min"])
        except (TypeError, ValueError):
            duration = 30

    end = _parse_event_datetime(extraction.get("confirmed_end_iso"))
    if end is None or end <= start:
        end = start + timedelta(minutes=duration)

    business_name = (booking_row or {}).get("title") or "Appointment"
    business_phone = (booking_row or {}).get("phone") or ""
    target_type = (booking_row or {}).get("target_type") or ""
    special_instructions = (booking_row or {}).get("special_instructions") or ""

    creds = load_credentials()
    if not creds:
        raise RuntimeError(
            "No Google Calendar credentials. Reconnect at /calendar/auth "
            "(calendar event permission is now required)."
        )

    granted = {str(s) for s in (creds.scopes or [])}
    if "https://www.googleapis.com/auth/calendar.events" not in granted:
        raise RuntimeError(
            "Calendar is connected read-only. Disconnect and reconnect at "
            "/calendar/auth so BookAppt can add events."
        )

    tz_name = _local_tz_name()
    summary = f"{business_name}" if not target_type else f"{target_type.title()} — {business_name}"
    body = {
        "summary": summary,
        "description": _build_event_description(
            business_name=business_name,
            business_phone=business_phone,
            target_type=target_type,
            special_instructions=special_instructions,
            summary=str(extraction.get("summary") or ""),
            price_mentioned=str(extraction.get("price_mentioned") or ""),
            business_conditions=str(extraction.get("business_conditions") or ""),
        ),
        "start": {
            # Google Calendar API requires either a UTC offset or a timezone name.
            # Formatting as a naive local datetime with timezone name is the
            # cleanest way to avoid year/day shifts from UTC conversion.
            "dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": tz_name,
        },
        "end": {
            "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": tz_name,
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ],
        },
    }

    service = build("calendar", "v3", credentials=creds)
    created = service.events().insert(calendarId="primary", body=body).execute()
    link = created.get("htmlLink") or created.get("id")
    LOGGER.info("calendar event created id=%s link=%s", created.get("id"), link)
    return link
