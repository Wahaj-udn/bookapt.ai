#!/usr/bin/env python3
"""Twilio Media Stream <-> Gemini Live bridge — generalized from Carecaller's
gemini_bridge.py.

Key differences from Carecaller's version:
- Mission prompt is built generically from a `BookingRequest` instead of a
  fixed healthcare mission-prompt file with 14 canonical questions.
- A `check_slot_fits` tool (slot_matcher.SlotMatcher) is registered with
  the Live session. The model is instructed to always call this tool
  before accepting/rejecting a proposed time — the accept/reject decision
  is deterministic code, never LLM judgment.
- The bridge does NOT decide the call's outcome (booked / held / etc.)
  live. It only conducts the conversation and writes the conversation log.
  Outcome classification (including detecting a "hold" situation) happens
  post-call in extractor.py, reading the full transcript — this keeps the
  bridge simple and keeps outcome logic in one place, testable against
  transcripts rather than live state.
- No healthcare disclaimers, no fixed question list.

Environment: see config.py. Requires GEMINI_API_KEY, VOXLAYER_MEDIA_STREAM_URL
public wiring exactly as Carecaller's bridge did.
"""

from __future__ import annotations

import asyncio
try:
    import audioop
except ModuleNotFoundError:  # Python 3.13+ removed audioop from stdlib
    import audioop_lts as audioop  # pip install audioop-lts
import base64
import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import websockets
from google import genai
from google.genai import types

from .config import Config
from .models import BookingRequest
from .slot_matcher import SlotMatcher

LOGGER = logging.getLogger("voxlayer-bridge")


def _parse_sample_rate(mime_type: Optional[str], default: int) -> int:
    if not mime_type:
        return default
    match = re.search(r"rate=(\d+)", mime_type)
    return int(match.group(1)) if match else default


def twilio_payload_to_pcm16_16k(payload_b64: str) -> bytes:
    ulaw = base64.b64decode(payload_b64)
    pcm8 = audioop.ulaw2lin(ulaw, 2)
    pcm16k, _ = audioop.ratecv(pcm8, 2, 1, 8000, 16000, None)
    return pcm16k


def pcm_to_twilio_payload(pcm_bytes: bytes, input_rate: int) -> str:
    if input_rate != 8000:
        pcm_bytes, _ = audioop.ratecv(pcm_bytes, 2, 1, input_rate, 8000, None)
    ulaw = audioop.lin2ulaw(pcm_bytes, 2)
    return base64.b64encode(ulaw).decode("ascii")


def pcm16_rms(pcm_bytes: bytes) -> int:
    if not pcm_bytes:
        return 0
    return int(audioop.rms(pcm_bytes, 2))


def build_mission_prompt(booking: BookingRequest) -> str:
    """Generic negotiation mission prompt, filled from BookingRequest.

    This replaces Carecaller's fixed mission_prompt_healthcare.txt. Keep
    this generic — no target-type-specific branching beyond what's already
    in booking.target_type / special_instructions.
    """
    slot_lines = "\n".join(
        f"  - {s.start.isoformat()} to {s.end.isoformat()}" for s in booking.fitting_slots
    ) or "  (no fitting windows provided — treat any proposed slot as needing the tool check anyway)"

    followup_clause = ""
    if booking.is_followup_call and booking.prior_offer_summary:
        followup_clause = f"\n\nIMPORTANT CONTEXT FOR THIS CALL:\n{booking.prior_offer_summary}\n"

    return f"""You are an AI assistant calling on behalf of {booking.user_display_name} to book an
appointment with {booking.business_name}, a {booking.target_type or "business"}.

YOUR GOAL:
Negotiate and confirm one specific appointment date and time that fits your
client's calendar and deadline. You are polite, efficient, and sound natural
on the phone — like a real assistant, not a script reader.
{followup_clause}
HARD RULE — NEVER DECIDE CALENDAR FIT YOURSELF:
Whenever the business proposes a specific date and time, you MUST call the
`check_slot_fits` tool with that proposed start/end time before saying
anything that commits to accepting or rejecting it. Never reason about
whether a time "sounds fine" — always call the tool and act only on its
result. If the tool says the slot fits, accept it immediately and clearly
restate the confirmed date and time back to the business. If the tool says
it does not fit, do not accept it.

CLIENT'S CONSTRAINTS (for your own context only — DO NOT read this list
aloud to the business):
- Required appointment duration: {booking.required_duration_minutes} minutes
- Must be scheduled on or before: {booking.max_date.isoformat()}
- Known open calendar windows:
{slot_lines}

WHEN A PROPOSED SLOT DOES NOT FIT (per the tool's result):
1. Ask if there is any other time available, and try the tool again on any
   new proposal.
2. If, after a reasonable back-and-forth, the business's best offer still
   does not fit, do the following EXACTLY:
   a. Ask the business to hold that specific time tentatively for you.
   b. Say something like: "That's helpful, thank you. Let me confirm with
      my client and I'll call you right back." Do not say the appointment
      is confirmed.
   c. End the call politely.
3. Do not keep negotiating indefinitely — if the business cannot offer
   anything and cannot hold a tentative slot either, politely end the call
   and note that no availability could be found.

IF THE BUSINESS CANNOT HOLD A SLOT WITHOUT immediate confirmation:
Say: "I understand — let me quickly check with my client and I'll call you
right back with a confirmation." Then end the call politely. Do not commit
to the slot yourself.

SPECIAL INSTRUCTIONS FROM THE CLIENT TO RELAY TO THE BUSINESS:
{booking.special_instructions or "(none)"}

GUARDRAILS — NEVER DO THE FOLLOWING WITHOUT EXPLICIT CLIENT APPROVAL:
- Never agree to or quote a specific price as final/accepted on the
  client's behalf. You may note a price if the business mentions one.
- Never share or confirm sensitive personal, medical, financial, or
  insurance details beyond what's explicitly listed in the special
  instructions above.
- Never agree to a cancellation policy or any binding terms.
- If the business asks for information you don't have, say you'll have
  your client follow up directly.

Keep your turns short and natural. Ask one thing at a time. If the person
who answers indicates this is the wrong business or wrong number, apologize
and end the call politely.
"""


@dataclass
class BridgeState:
    stream_sid: str = ""
    call_sid: str = ""
    booking_id: str = ""
    model_is_speaking: bool = False
    conversation_file: str = ""
    call_start_monotonic: float = 0.0


def _extract_custom_parameters(start_payload: dict) -> dict[str, str]:
    raw = start_payload.get("customParameters")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    if isinstance(raw, list):
        params: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                if name:
                    params[name] = value
        return params
    return {}


class BridgeService:
    """One instance handles one live call. Create per-connection or reuse a
    single instance across connections (it holds no per-call mutable state
    of its own; state lives in BridgeState + the booking lookup)."""

    def __init__(self, gemini_api_key: str, booking_loader):
        """`booking_loader(booking_id: str) -> BookingRequest` is supplied by
        the host application — voxlayer does not know how bookings are
        stored (DB, file, in-memory queue, etc.)."""
        self._client = genai.Client(api_key=gemini_api_key)
        self._model = Config.get(Config.GEMINI_LIVE_MODEL, "gemini-3.1-flash-live-preview")
        self._voice_name = Config.get(Config.GEMINI_VOICE_NAME, "").strip()
        self._conversation_dir = Path(Config.data_path(Config.CONVERSATION_DIR, "conversation")).resolve()
        self._conversation_log_lock = asyncio.Lock()
        self._booking_loader = booking_loader

    async def handle_ws(self, websocket) -> None:
        state = BridgeState()
        LOGGER.info("Twilio stream connected from %s", getattr(websocket, "remote_address", None))

        # Wait for the 'start' event before we know which booking this is.
        booking: Optional[BookingRequest] = None
        matcher: Optional[SlotMatcher] = None

        async def handle_start(start_payload: dict) -> None:
            nonlocal booking, matcher
            state.stream_sid = start_payload.get("streamSid", "")
            state.call_sid = start_payload.get("callSid", "")
            state.call_start_monotonic = asyncio.get_running_loop().time()
            custom = _extract_custom_parameters(start_payload)
            state.booking_id = custom.get("booking_id", "").strip()
            state.conversation_file = self._build_conversation_file_path(state.call_sid)

            booking = self._booking_loader(state.booking_id)
            matcher = SlotMatcher(booking.fitting_slots, booking.max_date)

            await self._append_conversation_line(f"# call_sid={state.call_sid}", state.conversation_file)
            await self._append_conversation_line(f"# booking_id={state.booking_id}", state.conversation_file)

        # Peek the first Twilio message synchronously-ish to get the start event
        # before opening the Gemini Live session (we need the booking's tools
        # + system prompt before connecting).
        first_start_message = None
        async for raw in websocket:
            message = json.loads(raw)
            if message.get("event") == "start":
                first_start_message = message
                break
            if message.get("event") == "stop":
                return

        if first_start_message is None:
            LOGGER.warning("Stream ended before a start event was received")
            return

        await handle_start(first_start_message.get("start", {}))
        assert booking is not None and matcher is not None

        system_instruction = build_mission_prompt(booking)
        tool_declaration = {"function_declarations": [matcher.tool_schema()]}

        config: dict = {
            "response_modalities": ["AUDIO"],
            "system_instruction": system_instruction,
            "input_audio_transcription": {},
            "tools": [tool_declaration],
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "prefix_padding_ms": 120,
                    "silence_duration_ms": 450,
                }
            },
        }
        if self._voice_name:
            config["speech_config"] = {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self._voice_name}}
            }

        async with self._client.aio.live.connect(model=self._model, config=config) as session:
            kickoff = (
                f"Call context: you are calling {booking.business_name}. "
                f"Begin the call now by greeting them and stating you'd like to "
                f"book a {booking.target_type or 'appointment'}."
            )
            if booking.is_followup_call:
                kickoff += " This is a followup call — mention that you spoke with them before, per your context."
            await session.send_realtime_input(text=kickoff)

            twilio_to_gemini = asyncio.create_task(
                self._forward_twilio_to_gemini(websocket, session, state)
            )
            gemini_to_twilio = asyncio.create_task(
                self._forward_gemini_to_twilio(websocket, session, state, matcher)
            )

            while True:
                done, _ = await asyncio.wait(
                    [twilio_to_gemini, gemini_to_twilio], return_when=asyncio.FIRST_COMPLETED
                )
                if twilio_to_gemini in done:
                    exc = twilio_to_gemini.exception()
                    if exc:
                        raise exc
                    gemini_to_twilio.cancel()
                    break
                if gemini_to_twilio in done:
                    exc = gemini_to_twilio.exception()
                    if exc:
                        raise exc
                    gemini_to_twilio = asyncio.create_task(
                        self._forward_gemini_to_twilio(websocket, session, state, matcher)
                    )

        LOGGER.info("Twilio stream disconnected call_sid=%s", state.call_sid or "unknown")

    async def _forward_twilio_to_gemini(self, websocket, session, state: BridgeState) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            event = message.get("event")

            if event == "media":
                media = message.get("media", {})
                if media.get("track", "inbound") != "inbound":
                    continue
                payload_b64 = media.get("payload")
                if not payload_b64:
                    continue
                pcm16k = twilio_payload_to_pcm16_16k(payload_b64)
                if state.model_is_speaking and pcm16_rms(pcm16k) >= 700:
                    await self._send_twilio_clear(websocket, state.stream_sid)
                    state.model_is_speaking = False
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000")
                )
                continue

            if event == "stop":
                break

    async def _forward_gemini_to_twilio(self, websocket, session, state: BridgeState, matcher: SlotMatcher) -> None:
        gemini_buffer = ""

        async for response in session.receive():
            content = response.server_content
            tool_call = getattr(response, "tool_call", None)

            if tool_call and tool_call.function_calls:
                responses = []
                for fc in tool_call.function_calls:
                    if fc.name == "check_slot_fits":
                        result = matcher.resolve_tool_call(dict(fc.args or {}))
                        LOGGER.info("check_slot_fits(%s) -> %s", fc.args, result)
                        responses.append(
                            types.FunctionResponse(id=fc.id, name=fc.name, response=result)
                        )
                if responses:
                    await session.send_tool_response(function_responses=responses)
                continue

            if not content:
                continue

            if content.input_transcription and content.input_transcription.text:
                await self._emit_transcript("user", content.input_transcription.text, state)

            if content.output_transcription and content.output_transcription.text:
                gemini_buffer += content.output_transcription.text

            if content.model_turn and content.model_turn.parts:
                for part in content.model_turn.parts:
                    if not part.inline_data or not part.inline_data.data:
                        continue
                    input_rate = _parse_sample_rate(part.inline_data.mime_type, default=24000)
                    payload_b64 = pcm_to_twilio_payload(part.inline_data.data, input_rate=input_rate)
                    if not state.stream_sid:
                        continue
                    await websocket.send(
                        json.dumps(
                            {"event": "media", "streamSid": state.stream_sid, "media": {"payload": payload_b64}}
                        )
                    )
                    state.model_is_speaking = True

            if content.interrupted or content.turn_complete or content.generation_complete:
                state.model_is_speaking = False
                if gemini_buffer:
                    await self._emit_transcript("agent", gemini_buffer, state)
                    gemini_buffer = ""

        if gemini_buffer:
            await self._emit_transcript("agent", gemini_buffer, state)

    @staticmethod
    def _normalize_transcript(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    async def _emit_transcript(self, speaker: str, text: str, state: BridgeState) -> None:
        clean_text = self._normalize_transcript(text)
        if not clean_text:
            return
        LOGGER.info("%s> %s", speaker, clean_text)
        await self._append_conversation_line(f"{speaker}>{clean_text}", state.conversation_file)

    def _build_conversation_file_path(self, call_sid: str) -> str:
        safe_call_sid = re.sub(r"[^A-Za-z0-9_-]+", "_", (call_sid or "unknown").strip()) or "unknown"
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._conversation_dir.mkdir(parents=True, exist_ok=True)
        return str((self._conversation_dir / f"{timestamp}_{safe_call_sid}.txt").resolve())

    async def _append_conversation_line(self, line: str, conversation_file: str) -> None:
        async with self._conversation_log_lock:
            with open(conversation_file, "a", encoding="utf-8") as f:
                f.write(f"{line}\n")

    async def _send_twilio_clear(self, websocket, stream_sid: str) -> None:
        if not stream_sid:
            return
        await websocket.send(json.dumps({"event": "clear", "streamSid": stream_sid}))


async def run_server(host: str, port: int, path: str, service: BridgeService) -> None:
    async def ws_handler(websocket):
        request_path = getattr(websocket, "path", "")
        if path and request_path and request_path != path:
            await websocket.close(code=1008, reason="Invalid path")
            return
        await service.handle_ws(websocket)

    async with websockets.serve(ws_handler, host, port, ping_interval=20, max_size=2**22):
        LOGGER.info("voxlayer bridge listening on ws://%s:%s%s", host, port, path)
        await asyncio.Future()


def main(booking_loader) -> int:
    """`booking_loader` must be supplied by the host application at process
    start — e.g. a function reading a JSON file, a DB row, or a small
    in-memory dict keyed by booking_id."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    api_key = Config.get(Config.GEMINI_API_KEY)
    if not api_key:
        raise SystemExit("Missing GEMINI_API_KEY environment variable.")

    service = BridgeService(gemini_api_key=api_key, booking_loader=booking_loader)
    host = Config.get(Config.BRIDGE_HOST, "0.0.0.0")
    port = Config.get_int(Config.BRIDGE_PORT, 8765)
    path = Config.get(Config.BRIDGE_PATH, "/media-stream")

    try:
        asyncio.run(run_server(host, port, path, service))
    except KeyboardInterrupt:
        LOGGER.info("voxlayer bridge stopped by user")
    return 0


if __name__ == "__main__":
    # Minimal default booking_loader for standalone testing: reads a single
    # booking from a local JSON file path given by VOXLAYER_TEST_BOOKING_FILE.
    from .models import BookingRequest as _BR

    def _default_loader(booking_id: str) -> _BR:
        test_file = Config.get("VOXLAYER_TEST_BOOKING_FILE")
        if not test_file:
            raise RuntimeError(
                "No booking_loader supplied and VOXLAYER_TEST_BOOKING_FILE not set. "
                "The host application should call bridge.main(booking_loader=...) directly."
            )
        return _BR.from_json(Path(test_file).read_text(encoding="utf-8"))

    raise SystemExit(main(_default_loader))
