#!/usr/bin/env python3
"""Extraction — the piece with no Carecaller equivalent.

Carecaller's extract_responses.py did deterministic regex matching against
14 fixed questions. There's no fixed question list here, so this module
uses an LLM pass over the normalized transcript to produce the three-layer
result structure agreed for BookAppt.ai:

  1. outcome              -> trusted first from normalizer.py's `outcome=`
                              line (deterministic contract), cross-checked
                              here only as a fallback if that line is
                              missing/invalid.
  2. key fields            -> confirmed_start/end, price_mentioned,
                              held_offer_start/end, business_conditions
  3. summary               -> plain-paragraph narrative for the user

Output is a plain dict (JSON-serializable) rather than a CallResult, since
result_store.py is responsible for stitching in booking_id/call_sid/
recording metadata that this module doesn't have.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from google import genai

from .config import Config
from .models import NegotiationOutcome

OUTCOME_RE = re.compile(r"^outcome\s*=\s*([a-z_]+)\s*$", re.IGNORECASE)


def _fix_year(iso_value: str | None) -> str | None:
    """Safety-net: correct a Gemini-extracted ISO datetime that has the wrong year.

    Gemini doesn't know the current date, so when a transcript says "October 31st"
    it may guess 2024 or 2025. If the extracted year is more than 1 year in the
    past, replace it with the current year (preserving the month/day/time exactly).

    Returns the corrected ISO string, or the original if no fix is needed,
    or None/unchanged if the value is None or unparseable.
    """
    if not iso_value:
        return iso_value
    try:
        dt = datetime.datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        return iso_value  # unparseable — leave as-is for downstream handling

    today = datetime.date.today()
    # If the extracted year is more than 1 year behind today, snap to current year.
    if dt.year < today.year - 1:
        try:
            dt = dt.replace(year=today.year)
        except ValueError:
            # e.g. Feb 29 in a non-leap year — skip correction
            pass

    # Preserve original tzinfo format: strip if originally naive, keep if aware
    if "Z" in str(iso_value) or "+" in str(iso_value).split("T")[-1]:
        return dt.isoformat()
    return dt.replace(tzinfo=None).isoformat()


def _get_api_key() -> str:
    dedicated = Config.get(Config.NORMALIZER_API_KEY)
    return dedicated or Config.get(Config.GEMINI_API_KEY)


def _read_outcome_and_body(normalized_text: str) -> tuple[str, str]:
    lines = [ln for ln in normalized_text.splitlines() if ln.strip()]
    if not lines:
        return NegotiationOutcome.ESCALATE_TO_HUMAN.value, ""
    m = OUTCOME_RE.match(lines[0].strip())
    outcome = m.group(1).lower() if m and m.group(1).lower() in NegotiationOutcome.values() else NegotiationOutcome.ESCALATE_TO_HUMAN.value
    body = "\n".join(lines[1:]) if m else "\n".join(lines)
    return outcome, body


def build_extraction_prompt(body_transcript: str, outcome: str) -> str:
    today = datetime.date.today().isoformat()  # e.g. "2026-10-24"
    return f"""You are analyzing a normalized call transcript between an AI booking
assistant (AGENT) and a business (USER), for an appointment negotiation.
The call's classified outcome is: {outcome}

IMPORTANT: Today's date is {today}. Use this to resolve any date references in
the transcript (e.g. "October 31st", "this Saturday", "next week") to the
correct full ISO 8601 datetime with the right year. Do NOT use a past year.

Read the transcript below and return ONLY a single JSON object (no markdown
fences, no commentary) with exactly these fields:

{{
  "confirmed_start_iso": string or null,   // ISO 8601 datetime if a specific
                                            // appointment was confirmed/accepted
                                            // by the agent, else null
  "confirmed_end_iso": string or null,     // same, end time, else null
  "held_offer_start_iso": string or null,  // ISO 8601 if the agent asked the
                                            // business to HOLD a specific time
                                            // pending client approval (outcome
                                            // pending_user_approval), else null
  "held_offer_end_iso": string or null,
  "price_mentioned": string,               // any price/cost the business stated,
                                            // verbatim-ish (e.g. "$120" or
                                            // "around 80 dollars"), or "" if none
  "business_conditions": string,           // any conditions/requirements the
                                            // business stated (e.g. "bring ID",
                                            // "arrive 15 minutes early"), or ""
  "summary": string                        // a natural, plain-paragraph summary
                                            // (3-6 sentences) of what happened on
                                            // the call, written for the end user
                                            // (the client) to read later. Mention
                                            // the business's name/role if stated,
                                            // what was discussed, and the result.
}}

Rules:
- Only fill in a date/time field if the transcript actually states or clearly
  implies a specific date and time. If only a day of week or vague time is
  given without enough info to construct an ISO datetime, leave it null and
  mention the vague detail in "summary" instead.
- Always use the year from today's date ({today[:4]}) unless the transcript
  explicitly states a different year.
- Do not fabricate a price or condition that wasn't stated.
- "summary" must be in plain prose, no bullet points, no headers.

TRANSCRIPT:
{body_transcript}
"""


def _safe_json_parse(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


def extract(normalized_path: Path) -> dict[str, Any]:
    """Entry point used by server.py's post-call pipeline.

    Returns a dict matching the fields consumed by result_store.py /
    models.CallResult (minus booking_id/call_sid/recording metadata).
    """
    from .gemini_retry import call_with_retry

    normalized_text = normalized_path.read_text(encoding="utf-8", errors="ignore")
    outcome, body = _read_outcome_and_body(normalized_text)

    api_key = _get_api_key()
    model_name = Config.get(Config.EXTRACTOR_MODEL, "gemini-2.5-flash")
    fallback_model = Config.get("VOXLAYER_EXTRACTOR_FALLBACK_MODEL", "").strip()

    default_result: dict[str, Any] = {
        "outcome": outcome,
        "confirmed_start_iso": None,
        "confirmed_end_iso": None,
        "held_offer_start_iso": None,
        "held_offer_end_iso": None,
        "price_mentioned": "",
        "business_conditions": "",
        "summary": "",
        "transcript_text": body.strip(),
    }

    if not api_key or not body.strip():
        return default_result

    try:
        client = genai.Client(api_key=api_key)
        prompt = build_extraction_prompt(body, outcome)

        def _call_primary() -> str:
            resp = client.models.generate_content(model=model_name, contents=prompt)
            return resp.text or ""

        def _call_fallback() -> str:
            resp = client.models.generate_content(model=fallback_model, contents=prompt)
            return resp.text or ""

        raw_text = call_with_retry(
            _call_primary,
            label=f"extractor/{model_name}",
            fallback_fn=_call_fallback if fallback_model and fallback_model != model_name else None,
        )
        parsed = _safe_json_parse(raw_text)
    except Exception:
        parsed = {}

    if not parsed:
        return default_result

    default_result.update(
        {
            "confirmed_start_iso": _fix_year(parsed.get("confirmed_start_iso")),
            "confirmed_end_iso": _fix_year(parsed.get("confirmed_end_iso")),
            "held_offer_start_iso": _fix_year(parsed.get("held_offer_start_iso")),
            "held_offer_end_iso": _fix_year(parsed.get("held_offer_end_iso")),
            "price_mentioned": str(parsed.get("price_mentioned") or ""),
            "business_conditions": str(parsed.get("business_conditions") or ""),
            "summary": str(parsed.get("summary") or ""),
        }
    )
    return default_result

