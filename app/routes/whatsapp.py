#!/usr/bin/env python3
"""WhatsApp incoming message webhook — handles user replies from Twilio.

Registered at POST /whatsapp/incoming in the Flask app.
Configure this URL in the Twilio Console:
  Messaging → Senders → WhatsApp → Sandbox (or approved number)
  "When a message comes in": https://YOUR_NGROK/whatsapp/incoming

Supported user commands (case-insensitive):
  "callback"  → find the most recently pending booking and trigger followup call
  anything else / no reply → ignored (per spec, no reply = cancel)
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, request

LOGGER = logging.getLogger("bookapt.whatsapp")

whatsapp_bp = Blueprint("whatsapp", __name__, url_prefix="/whatsapp")


def _twiml_reply(body: str) -> Response:
    """Return a TwiML MessagingResponse with a simple text reply."""
    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
    resp.message(body)
    return Response(str(resp), mimetype="text/xml")


def _find_most_recent_pending_booking() -> str | None:
    """Scan negotiation_state files and return the booking_id of the most
    recently updated booking that is HELD_PENDING_APPROVAL, or None."""
    try:
        from bookapt import negotiation_state
        pending = negotiation_state.list_pending_approvals()
        if not pending:
            return None
        # Sort by updated_at descending
        pending_sorted = sorted(pending, key=lambda r: r.updated_at, reverse=True)
        return pending_sorted[0].booking_id
    except Exception as exc:
        LOGGER.error("Error finding pending bookings: %s", exc)
        return None


@whatsapp_bp.post("/incoming")
def incoming_whatsapp():
    """Handle an incoming WhatsApp message from the user."""
    body = (request.form.get("Body") or "").strip().lower()
    from_number = (request.form.get("From") or "").strip()

    LOGGER.info("WhatsApp incoming from=%s body=%r", from_number, body)

    # ── Only "callback" triggers action ─────────────────────────────────────
    if "callback" not in body:
        # No reply / unrecognised → silently acknowledge (no TwiML reply needed
        # but we return 200 so Twilio doesn't retry)
        return Response("", status=200)

    # ── Find the pending booking ─────────────────────────────────────────────
    booking_id = _find_most_recent_pending_booking()
    if not booking_id:
        return _twiml_reply(
            "⚠️ No pending booking found that's waiting for approval. "
            "Nothing to call back about!"
        )

    # ── Approve + place followup call ────────────────────────────────────────
    try:
        from bookapt import caller, negotiation_state
        from app.booking_loader import booking_loader

        # Mark the held offer as approved in negotiation state
        negotiation_state.approve_held_offer(booking_id)

        # Reconstruct the BookingRequest and place the followup call
        booking_request = booking_loader(booking_id)
        call_sid = caller.place_followup_call(booking_request)

        LOGGER.info(
            "WhatsApp callback: followup call placed booking_id=%s call_sid=%s",
            booking_id, call_sid,
        )

        return _twiml_reply(
            f"✅ Got it! The AI agent is calling back to confirm your appointment.\n"
            f"Call SID: {call_sid[:20]}…\n\n"
            f"Results will be sent here once the call ends."
        )

    except Exception as exc:
        LOGGER.error("WhatsApp callback failed booking_id=%s: %s", booking_id, exc)
        return _twiml_reply(
            f"❌ Sorry, something went wrong placing the callback: {exc}\n"
            f"Please try again or check the web dashboard."
        )
