#!/usr/bin/env python3
"""WhatsApp notification module for BookAppt.ai.

Sends call result summaries to the user's WhatsApp number via Twilio,
and handles their replies ("callback" → trigger followup call).

Environment variables (all in .env):
  USER_WHATSAPP_NUMBER   - user's number, e.g. +919876543210
  TWILIO_WHATSAPP_FROM   - Twilio sandbox or approved sender, e.g. +14155238886
  (Twilio creds shared from TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Config

LOGGER = logging.getLogger("voxlayer-whatsapp")


def _format_whatsapp_number(raw: str) -> str:
    """Ensure number has whatsapp: prefix."""
    raw = raw.strip()
    if not raw.startswith("whatsapp:"):
        return f"whatsapp:{raw}"
    return raw


def _twilio_client():
    from twilio.rest import Client
    sid = Config.get(Config.TWILIO_ACCOUNT_SID)
    token = Config.get(Config.TWILIO_AUTH_TOKEN)
    if not sid or not token:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
    return Client(sid, token)


def _build_message(
    outcome: str,
    business_name: str,
    confirmed_start: Optional[str],
    confirmed_end: Optional[str],
    held_offer_start: Optional[str],
    held_offer_end: Optional[str],
    summary: str,
) -> str:
    """Build a concise WhatsApp notification message."""
    lines = [f"*BookAppt.ai — Call Result*\n*Business:* {business_name}"]

    outcome_clean = outcome.replace("_", " ").title()

    if outcome == "booked":
        lines.append(f"✅ *Booked!*")
        if confirmed_start:
            date_part = confirmed_start[:10]
            time_part = confirmed_start[11:16]
            lines.append(f"📅 Date: {date_part}  🕐 Time: {time_part}")
            if confirmed_end:
                lines.append(f"⏱ Until: {confirmed_end[11:16]}")
    elif outcome == "pending_user_approval":
        lines.append(f"⚠️ *Hold offer — your approval needed*")
        if held_offer_start:
            lines.append(f"📅 Proposed: {held_offer_start[:10]} at {held_offer_start[11:16]}")
            if held_offer_end:
                lines.append(f"⏱ Until: {held_offer_end[11:16]}")
        lines.append("\nReply *callback* to have the AI call back and confirm this slot.")
    elif outcome == "no_availability":
        lines.append(f"❌ *No availability found.*")
    elif outcome == "voicemail":
        lines.append(f"📵 *Went to voicemail — no appointment booked.*")
    elif outcome == "wrong_number":
        lines.append(f"📵 *Wrong number — please check the provider's phone.*")
    else:
        lines.append(f"ℹ️ *Status: {outcome_clean}*")

    if summary:
        # Trim to keep message short
        short_summary = summary[:300] + ("…" if len(summary) > 300 else "")
        lines.append(f"\n_{short_summary}_")

    lines.append("\n_BookAppt.ai — your AI booking assistant_")
    return "\n".join(lines)


def send_result_notification(
    booking_id: str,
    outcome: str,
    business_name: str,
    confirmed_start: Optional[str] = None,
    confirmed_end: Optional[str] = None,
    held_offer_start: Optional[str] = None,
    held_offer_end: Optional[str] = None,
    summary: str = "",
) -> bool:
    """Send a WhatsApp notification with the call result.

    Returns True if sent, False if not configured or failed.
    """
    to_number = Config.get("USER_WHATSAPP_NUMBER", "").strip()
    from_number = Config.get("TWILIO_WHATSAPP_FROM", "").strip()

    if not to_number or not from_number:
        LOGGER.debug("WhatsApp notification skipped: USER_WHATSAPP_NUMBER or TWILIO_WHATSAPP_FROM not set")
        return False

    message_body = _build_message(
        outcome=outcome,
        business_name=business_name,
        confirmed_start=confirmed_start,
        confirmed_end=confirmed_end,
        held_offer_start=held_offer_start,
        held_offer_end=held_offer_end,
        summary=summary,
    )

    try:
        client = _twilio_client()
        msg = client.messages.create(
            body=message_body,
            from_=_format_whatsapp_number(from_number),
            to=_format_whatsapp_number(to_number),
        )
        LOGGER.info("WhatsApp notification sent for booking_id=%s sid=%s", booking_id, msg.sid)
        return True
    except Exception as exc:
        LOGGER.error("WhatsApp notification failed for booking_id=%s: %s", booking_id, exc)
        return False
