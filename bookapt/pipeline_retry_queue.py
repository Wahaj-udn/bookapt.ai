#!/usr/bin/env python3
"""Pipeline retry queue — persistence for failed post-call pipeline runs.

When the post-call pipeline fails after all retries (e.g. Gemini is down
for several minutes), the failed entry is written to a JSON file so it
can be retried later via the /admin/retry-pipeline Flask endpoint without
losing any call data.

Queue file location: <VOXLAYER_DATA_DIR>/failed_pipeline_queue.json

Each entry in the queue is a dict:
  {
    "booking_id":     str,
    "call_sid":       str,
    "recording_file": str (absolute path),
    "failed_at":      str (ISO datetime),
    "error":          str,
    "attempts":       int,
  }
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .config import Config

LOGGER = logging.getLogger("voxlayer-pipeline-queue")
_LOCK = threading.Lock()  # protect concurrent writes from parallel pipeline threads


def _queue_path() -> Path:
    data_dir = Path(Config.get(Config.DATA_DIR, "voxlayer_data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "failed_pipeline_queue.json"


def _load() -> list[dict[str, Any]]:
    p = _queue_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list[dict[str, Any]]) -> None:
    _queue_path().write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def enqueue_failure(
    booking_id: str,
    call_sid: str,
    recording_file: Path,
    error: str,
) -> None:
    """Add a failed pipeline run to the retry queue."""
    entry: dict[str, Any] = {
        "booking_id": booking_id,
        "call_sid": call_sid,
        "recording_file": str(recording_file),
        "failed_at": dt.datetime.utcnow().isoformat(),
        "error": error,
        "attempts": 1,
    }
    with _LOCK:
        entries = _load()
        # De-duplicate: update existing entry for same call_sid if present
        for existing in entries:
            if existing.get("call_sid") == call_sid:
                existing["error"] = error
                existing["failed_at"] = entry["failed_at"]
                existing["attempts"] = existing.get("attempts", 1) + 1
                _save(entries)
                LOGGER.info(
                    "pipeline-queue: updated retry entry for call_sid=%s (attempt %d)",
                    call_sid, existing["attempts"],
                )
                return
        entries.append(entry)
        _save(entries)
    LOGGER.info("pipeline-queue: enqueued failed pipeline for call_sid=%s", call_sid)


def list_queue() -> list[dict[str, Any]]:
    """Return current queue entries (snapshot)."""
    with _LOCK:
        return list(_load())


def remove_entry(call_sid: str) -> bool:
    """Remove a successfully-retried entry from the queue. Returns True if found."""
    with _LOCK:
        entries = _load()
        new_entries = [e for e in entries if e.get("call_sid") != call_sid]
        if len(new_entries) == len(entries):
            return False
        _save(new_entries)
    return True


def retry_all(app) -> dict[str, Any]:
    """Re-run the post-call pipeline for every entry in the queue.

    Called from the /admin/retry-pipeline Flask endpoint.
    Each entry is attempted once; successes are removed, failures stay.

    Returns a summary dict: {"retried": int, "succeeded": int, "failed": int}
    """
    from . import extractor, normalizer, result_store, transcript_builder, whatsapp_notifier
    from .config import Config as Cfg

    with _LOCK:
        entries = _load()

    if not entries:
        return {"retried": 0, "succeeded": 0, "failed": 0, "entries": []}

    succeeded, failed_list = 0, []

    for entry in entries:
        call_sid = entry["call_sid"]
        booking_id = entry["booking_id"]
        recording_file = Path(entry["recording_file"])

        try:
            # Skip if recording is gone
            if not recording_file.exists():
                LOGGER.warning(
                    "pipeline-queue: recording file missing for call_sid=%s, removing from queue",
                    call_sid,
                )
                remove_entry(call_sid)
                continue

            # Re-run from transcript_builder onward (Whisper already ran or
            # we re-run it if the whisper file is also gone)
            from . import transcriber
            from .config import Config as Cfg
            whisper_dir = Path(Cfg.data_path(Cfg.WHISPER_TRANSCRIPT_DIR, "whisper_transcript")).resolve()
            # Try to find existing whisper file by recording stem
            existing_whisper = list(whisper_dir.glob(f"*{recording_file.stem}*.txt"))
            if existing_whisper:
                whisper_path = existing_whisper[0]
                app.logger.info("pipeline-queue: reusing whisper transcript %s", whisper_path)
            else:
                whisper_path = transcriber.transcribe(recording_file)
                app.logger.info("pipeline-queue: re-transcribed -> %s", whisper_path)

            final_path = transcript_builder.build(whisper_path, call_sid=call_sid)
            if final_path is None:
                raise RuntimeError(f"No conversation log found for call_sid={call_sid}")

            normalized_path = normalizer.normalize(final_path)
            extraction = extractor.extract(normalized_path)

            result_store.record_call_result(
                booking_id=booking_id,
                call_sid=call_sid,
                recording_file=recording_file,
                normalized_path=normalized_path,
                extraction=extraction,
            )

            # WhatsApp notification
            try:
                business_name = "the provider"
                try:
                    from app import db as app_db
                    row = app_db.get_booking(booking_id)
                    if row:
                        business_name = row.get("title", business_name)
                except Exception:
                    pass
                whatsapp_notifier.send_result_notification(
                    booking_id=booking_id,
                    outcome=extraction.get("outcome", ""),
                    business_name=business_name,
                    confirmed_start=extraction.get("confirmed_start_iso"),
                    confirmed_end=extraction.get("confirmed_end_iso"),
                    held_offer_start=extraction.get("held_offer_start_iso"),
                    held_offer_end=extraction.get("held_offer_end_iso"),
                    summary=extraction.get("summary", ""),
                )
            except Exception as wa_exc:
                app.logger.warning("pipeline-queue: WhatsApp notification error: %s", wa_exc)

            remove_entry(call_sid)
            succeeded += 1
            app.logger.info("pipeline-queue: retry succeeded for call_sid=%s", call_sid)

        except Exception as exc:
            app.logger.error("pipeline-queue: retry failed for call_sid=%s: %s", call_sid, exc)
            # Update attempt count in queue
            enqueue_failure(booking_id, call_sid, recording_file, str(exc))
            failed_list.append({"call_sid": call_sid, "error": str(exc)})

    return {
        "retried": len(entries),
        "succeeded": succeeded,
        "failed": len(failed_list),
        "failed_entries": failed_list,
    }
