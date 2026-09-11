#!/usr/bin/env python3
"""Aggregate per-call results into a persistent store, and advance the
booking's negotiation state accordingly.

Generalized from Carecaller's build_result_json.py. Differences:
- Keyed by call_sid (dedup) and booking_id (grouping) instead of a single
  flat "transcripts" list scanned by transcript_text equality.
- One JSON file per booking (`results/<booking_id>.json`) containing the
  full call history for that booking, rather than one giant global file —
  this matches BookAppt's "history section per booking" dashboard need
  and avoids one huge file being rewritten on every call.
- After recording a result, updates negotiation_state: BOOKED/NO_AVAILABILITY/
  etc. are terminal; PENDING_USER_APPROVAL stamps the held offer window so
  the dashboard alert + later approve_held_offer()/place_followup_call()
  flow has what it needs.
- A separate `results/_index.json` gives an at-a-glance list for the
  dashboard's history table without opening every per-booking file.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Optional

from mutagen.mp3 import MP3

from . import negotiation_state
from .config import Config
from .models import CallResult, NegotiationOutcome


def _results_dir() -> Path:
    path = Path(Config.data_path(Config.RESULTS_DIR, "results")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _booking_file(booking_id: str) -> Path:
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in booking_id)
    return _results_dir() / f"{safe_id}.json"


def _index_file() -> Path:
    return _results_dir() / "_index.json"


def _get_call_duration_seconds(recording_file: Path) -> int:
    try:
        length = MP3(str(recording_file)).info.length
        return int(round(float(length))) if length else 0
    except Exception:
        return 0


def _parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_booking_history(booking_id: str) -> dict[str, Any]:
    path = _booking_file(booking_id)
    if not path.exists():
        return {"booking_id": booking_id, "calls": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("calls"), list):
            return data
    except Exception:
        pass
    return {"booking_id": booking_id, "calls": []}


def _save_booking_history(booking_id: str, history: dict[str, Any]) -> None:
    _booking_file(booking_id).write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _update_index(booking_id: str, latest_call: dict[str, Any]) -> None:
    index_path = _index_file()
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        if not isinstance(index, dict):
            index = {}
    except Exception:
        index = {}

    index[booking_id] = {
        "booking_id": booking_id,
        "latest_outcome": latest_call.get("outcome"),
        "latest_call_sid": latest_call.get("call_sid"),
        "latest_summary": latest_call.get("summary"),
        "updated_at": dt.datetime.utcnow().isoformat(),
    }
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def record_call_result(
    booking_id: str,
    call_sid: str,
    recording_file: Path,
    normalized_path: Path,
    extraction: dict[str, Any],
) -> dict[str, Any]:
    """Append one call's result to the booking's history, update the index,
    and advance negotiation_state. Idempotent per call_sid."""

    history = _load_booking_history(booking_id)
    existing_sids = {c.get("call_sid") for c in history["calls"]}
    if call_sid in existing_sids:
        return next(c for c in history["calls"] if c.get("call_sid") == call_sid)

    outcome_str = extraction.get("outcome", NegotiationOutcome.ESCALATE_TO_HUMAN.value)
    try:
        outcome = NegotiationOutcome(outcome_str)
    except ValueError:
        outcome = NegotiationOutcome.ESCALATE_TO_HUMAN

    result = CallResult(
        booking_id=booking_id,
        call_sid=call_sid,
        outcome=outcome,
        confirmed_start=_parse_iso(extraction.get("confirmed_start_iso")),
        confirmed_end=_parse_iso(extraction.get("confirmed_end_iso")),
        price_mentioned=str(extraction.get("price_mentioned") or ""),
        held_offer_start=_parse_iso(extraction.get("held_offer_start_iso")),
        held_offer_end=_parse_iso(extraction.get("held_offer_end_iso")),
        business_conditions=str(extraction.get("business_conditions") or ""),
        summary=str(extraction.get("summary") or ""),
        transcript_text=str(extraction.get("transcript_text") or ""),
        recording_path=str(recording_file),
        call_duration_seconds=_get_call_duration_seconds(recording_file),
        started_at=dt.datetime.utcnow(),
        raw_extra={"normalized_transcript_path": str(normalized_path)},
    )

    # Persist as dict with extra metadata not in CallResult schema
    call_record = result.to_dict()
    call_record["normalized_transcript_path"] = str(normalized_path)
    call_record["recorded_at"] = dt.datetime.utcnow().isoformat()

    history["calls"].append(call_record)
    _save_booking_history(booking_id, history)
    _update_index(booking_id, call_record)

    # --- advance negotiation state ---
    if outcome == NegotiationOutcome.PENDING_USER_APPROVAL:
        if result.held_offer_start and result.held_offer_end:
            negotiation_state.mark_held_pending_approval(
                booking_id, result.held_offer_start, result.held_offer_end,
                reason="business offer outside auto-accept rules"
            )
        else:
            # Held outcome without parseable times is treated as needing a
            # human to look at it rather than silently dropped.
            negotiation_state.mark_resolved(booking_id, NegotiationOutcome.ESCALATE_TO_HUMAN)
    else:
        negotiation_state.mark_resolved(booking_id, outcome)

    return call_record


def get_booking_history(booking_id: str) -> dict[str, Any]:
    """Used by the dashboard's history section for one booking."""
    return _load_booking_history(booking_id)


def get_history_index() -> dict[str, Any]:
    """Used by the dashboard's history table (all bookings at a glance)."""
    index_path = _index_file()
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
