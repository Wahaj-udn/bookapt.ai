#!/usr/bin/env python3
"""Outbound call placement — generalized from Carecaller's call.py / csv_call_queue.py.

No CSV batch queue here by design (BookAppt.ai negotiates one booking at a
time). This module exposes two entry points:

    place_call(booking)           -> first call for a booking
    place_followup_call(booking)  -> the "I confirmed it" callback after
                                      user approval of a held offer

Both just wrap Twilio's REST API and stamp negotiation_state so the rest
of the pipeline (bridge.py, result_store.py) can look up context by
call_sid later.
"""

from __future__ import annotations

from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from twilio.rest import Client

from . import negotiation_state
from .config import Config
from .models import BookingRequest


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _append_query(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _twilio_client() -> Client:
    sid = Config.get(Config.TWILIO_ACCOUNT_SID)
    token = Config.get(Config.TWILIO_AUTH_TOKEN)
    if not sid or not token:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in environment.")
    return Client(sid, token)


def _build_twiml_url(booking: BookingRequest) -> str:
    base = Config.get(Config.OUTBOUND_TWIML_URL)
    if not base:
        raise RuntimeError("Missing VOXLAYER_OUTBOUND_TWIML_URL in environment.")
    return _append_query(
        base,
        {
            "booking_id": booking.booking_id,
            "target_type": booking.target_type,
            "is_followup": "1" if booking.is_followup_call else "0",
        },
    )


def _place(booking: BookingRequest) -> str:
    client = _twilio_client()
    from_number = booking.call_from_number or Config.get(Config.CALL_FROM_NUMBER)
    if not from_number:
        raise RuntimeError("No from-number: set VOXLAYER_CALL_FROM_NUMBER or booking.call_from_number.")

    kwargs: dict = {
        "to": booking.business_phone,
        "from_": from_number,
        "url": _build_twiml_url(booking),
    }

    if Config.get_bool(Config.RECORD_CALLS, True):
        kwargs["record"] = True
        kwargs["recording_channels"] = "mono"
        kwargs["recording_status_callback_event"] = ["completed"]
        callback = Config.get(Config.RECORDING_STATUS_CALLBACK_URL)
        if callback:
            kwargs["recording_status_callback"] = _append_query(
                callback, {"booking_id": booking.booking_id}
            )
            kwargs["recording_status_callback_method"] = "POST"

    call = client.calls.create(**kwargs)
    return str(call.sid)


def place_call(booking: BookingRequest) -> str:
    """Place the first negotiation call for a booking.

    Returns the Twilio call SID. The bridge (bridge.py) will receive the
    media stream and drive the actual conversation; this function's only
    job is dialing and stamping state.
    """
    booking.is_followup_call = False
    call_sid = _place(booking)
    negotiation_state.mark_call_started(booking.booking_id, call_sid)
    return call_sid


def place_followup_call(booking: BookingRequest) -> str:
    """Place the callback after the user has approved a held offer.

    Caller is responsible for having already checked
    `negotiation_state.is_pending_approval(booking.booking_id)` and calling
    `negotiation_state.approve_held_offer(...)` — this function assumes
    that's already been done and just dials, filling
    `booking.prior_offer_summary` from state for the mission prompt.
    """
    record = negotiation_state.load(booking.booking_id)
    booking.is_followup_call = True
    booking.prior_offer_summary = negotiation_state.build_prior_offer_summary(record)
    call_sid = _place(booking)
    negotiation_state.mark_call_started(booking.booking_id, call_sid)
    return call_sid
