#!/usr/bin/env python3
"""Twilio voice webhook server — generalized from Carecaller's server.py.

Endpoints:
- GET  /health
- POST /voice/outbound     -> TwiML that connects the call to bridge.py's media stream
- POST /voice/recording    -> Twilio recording-completed callback; downloads MP3
                              and kicks off the post-call pipeline

No healthcare-specific routes, no CSV-queue event handling. `booking_id`
flows through as a custom TwiML stream parameter and a query param, so the
bridge and the post-call pipeline can both recover it.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import re
import threading
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from flask import Flask, Response, jsonify, request
from twilio.twiml.voice_response import Connect, VoiceResponse

from .config import Config


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_token(value: str, fallback: str = "unknown") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip())
    return cleaned or fallback


def _download_recording_mp3(recording_url: str, output_file: Path, account_sid: str, auth_token: str) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    auth_bytes = f"{account_sid}:{auth_token}".encode("utf-8")
    auth_header = base64.b64encode(auth_bytes).decode("ascii")
    req = urlrequest.Request(
        recording_url,
        headers={"Authorization": f"Basic {auth_header}"},
        method="GET",
    )
    with urlrequest.urlopen(req, timeout=30) as response:
        content = response.read()
    output_file.write_bytes(content)


def _run_post_call_pipeline(app: Flask, recording_file: Path, booking_id: str, call_sid: str) -> None:
    """Runs transcription -> alignment -> normalization -> extraction ->
    result aggregation in a background thread, mirroring server.py's
    `_start_transcription_job` chain in Carecaller."""

    def _job() -> None:
        from . import extractor, normalizer, result_store, transcriber, transcript_builder

        try:
            whisper_path = transcriber.transcribe(recording_file)
            app.logger.info("voxlayer: whisper transcript saved -> %s", whisper_path)

            if not Config.get_bool(Config.AUTO_BUILD_FINAL_TRANSCRIPT, True):
                return
            final_path = transcript_builder.build(whisper_path, call_sid=call_sid)
            if final_path is None:
                app.logger.warning("voxlayer: no matching conversation log for call_sid=%s", call_sid)
                return
            app.logger.info("voxlayer: final transcript saved -> %s", final_path)

            if not Config.get_bool(Config.AUTO_NORMALIZE, True):
                return
            normalized_path = normalizer.normalize(final_path)
            app.logger.info("voxlayer: normalized transcript saved -> %s", normalized_path)

            if not Config.get_bool(Config.AUTO_EXTRACT, True):
                return
            extraction = extractor.extract(normalized_path)
            app.logger.info("voxlayer: extraction complete for call_sid=%s outcome=%s", call_sid, extraction.get("outcome"))

            if not Config.get_bool(Config.AUTO_UPDATE_RESULTS, True):
                return
            result_store.record_call_result(
                booking_id=booking_id,
                call_sid=call_sid,
                recording_file=recording_file,
                normalized_path=normalized_path,
                extraction=extraction,
            )
            app.logger.info("voxlayer: result recorded for booking_id=%s", booking_id)

            # ── WhatsApp notification ────────────────────────────────────────
            try:
                from . import whatsapp_notifier
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
                app.logger.warning("voxlayer: WhatsApp notification error: %s", wa_exc)

        except Exception as exc:  # pragma: no cover - defensive background job
            app.logger.error("voxlayer: post-call pipeline failed for call_sid=%s: %s", call_sid, exc)
            # Layer 2: persist to retry queue so the admin endpoint can re-run it
            try:
                from .pipeline_retry_queue import enqueue_failure
                enqueue_failure(booking_id, call_sid, recording_file, str(exc))
                app.logger.info("voxlayer: call_sid=%s added to retry queue", call_sid)
            except Exception as qe:
                app.logger.error("voxlayer: could not enqueue retry for call_sid=%s: %s", call_sid, qe)


    threading.Thread(target=_job, daemon=True, name=f"voxlayer-postcall-{call_sid}").start()


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health() -> Response:
        return jsonify({"ok": True, "service": "voxlayer-voice-webhook"})

    @app.post("/voice/outbound")
    def outbound_voice() -> Response:
        response = VoiceResponse()
        stream_url = Config.get(Config.MEDIA_STREAM_URL)
        booking_id = (request.values.get("booking_id") or "").strip()
        target_type = (request.values.get("target_type") or "").strip()
        is_followup = (request.values.get("is_followup") or "0").strip()

        if stream_url:
            connect = Connect()
            stream = connect.stream(url=stream_url, name="voxlayer-outbound")
            if booking_id:
                stream.parameter(name="booking_id", value=booking_id)
            if target_type:
                stream.parameter(name="target_type", value=target_type)
            stream.parameter(name="is_followup", value=is_followup)
            response.append(connect)
            response.pause(length=600)
        else:
            response.say("Voice stream is not configured. Please set VOXLAYER_MEDIA_STREAM_URL.")
            response.hangup()

        return Response(str(response), mimetype="text/xml")

    @app.post("/voice/recording")
    @app.post("/voice/recording/")
    def recording_events() -> Response:
        event = dict(request.form)
        call_sid = event.get("CallSid", "unknown")
        recording_sid = event.get("RecordingSid", "unknown")
        recording_status = (event.get("RecordingStatus") or "").strip().lower()
        recording_url_base = (event.get("RecordingUrl") or "").strip()
        booking_id = (request.args.get("booking_id") or event.get("booking_id") or "").strip()

        if recording_status != "completed":
            return ("", 204)
        if not recording_url_base:
            return ("missing RecordingUrl", 400)

        account_sid = Config.get(Config.TWILIO_ACCOUNT_SID)
        auth_token = Config.get(Config.TWILIO_AUTH_TOKEN)
        if not account_sid or not auth_token:
            app.logger.error("voxlayer: cannot download recording, Twilio credentials not configured")
            return ("twilio credentials not configured", 500)

        recordings_dir = Path(Config.data_path(Config.RECORDINGS_DIR, "recordings")).resolve()
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{timestamp}_{_safe_token(call_sid, 'call')}_{_safe_token(recording_sid, 'recording')}.mp3"
        output_file = recordings_dir / filename

        if output_file.exists():
            return ("", 204)

        try:
            _download_recording_mp3(
                recording_url=f"{recording_url_base}.mp3",
                output_file=output_file,
                account_sid=account_sid,
                auth_token=auth_token,
            )
            app.logger.info("voxlayer: saved recording -> %s", output_file)
            if Config.get_bool(Config.AUTO_TRANSCRIBE, True):
                _run_post_call_pipeline(app, output_file, booking_id, call_sid)
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            app.logger.error("voxlayer: failed to download recording sid=%s: %s", recording_sid, exc)
            return ("failed to download recording", 502)

        return ("", 204)

    return app


def main() -> int:
    app = create_app()
    host = Config.get(Config.HOST, "0.0.0.0")
    port = Config.get_int(Config.PORT, 5000)
    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
