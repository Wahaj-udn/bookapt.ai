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
    """Generic negotiation mission prompt, filled from BookingRequest."""
    slot_lines = "\n".join(
        f"  - {s.start.isoformat()} to {s.end.isoformat()}" for s in booking.fitting_slots
    ) or "  (none — use the tool to check any proposed slot against the deadline)"

    has_slots = bool(booking.fitting_slots)
    slot_guidance = (
        "When YOU need to suggest a time, only propose times that fall within "
        "the Known open calendar windows listed below. Never suggest a time "
        "outside those windows yourself."
        if has_slots else
        "No pre-computed windows are available. When you suggest a time, "
        "ensure it falls on or before the max date and meets the duration."
    )

    followup_clause = ""
    if booking.is_followup_call and booking.prior_offer_summary:
        followup_clause = f"\n\nIMPORTANT CONTEXT FOR THIS CALL:\n{booking.prior_offer_summary}\n"

    return f"""You are an AI assistant calling on behalf of {booking.user_display_name} defualt user name is VIKAS to book an
appointment with {booking.business_name}, a {booking.target_type or "business"}.
{followup_clause}
YOUR GOAL:
Confirm one specific appointment date and time. Speak naturally, like a
real human assistant — NOT like a script reader or a robot.

CONVERSATION PACING — CRITICAL:
- Your very first line must be ONE brief, natural sentence: greet them,
  say why you're calling, and STOP. Wait for them to respond before
  saying anything else. Example: "Hi, I'm calling to book a
  {booking.target_type or 'appointment'} for my client — do you have
  any availability this week?"
- Keep every turn to 1–2 sentences maximum. Ask ONE question at a time.
- Do NOT front-load the reason, the duration, the date, and the special
  instructions all in the first sentence. Let the conversation unfold
  naturally.
- Pause and listen after each sentence. Do not speak again until the
  other person has responded.

CLIENT'S CONSTRAINTS (for your own context only — DO NOT read this list
aloud to the business):
- Required appointment duration: {booking.required_duration_minutes} minutes
- Must be scheduled on or before: {booking.max_date.isoformat()}
- Known open calendar windows:
{slot_lines}

HOW TO HANDLE TIME PROPOSALS:

{slot_guidance}

There are TWO kinds of time proposals — treat them DIFFERENTLY:

1. TIME YOU PROPOSE TO THE BUSINESS:
   You have already checked that your suggestion fits the calendar.
   If the business agrees, accept it immediately and confirm it.
   Do NOT call `check_slot_fits` on a time you yourself suggested.
   NEVER reverse a time you proposed — if you asked for 3 PM and
   they said yes, the answer is yes.

2. TIME THE BUSINESS PROPOSES TO YOU:
   You MUST call `check_slot_fits` before accepting or rejecting it.
   Never say "yes" or "no" until the tool has returned a result.
   Act only on the tool's answer — not your own judgment.

WHEN `check_slot_fits` RETURNS fits=False (business-proposed time):
1. Politely say that time doesn't quite work for your client and ask
   if there is any other availability.
2. Try the tool again on any new proposal from the business.
3. After a reasonable back-and-forth with no match, ask the business
   to hold the best offer tentatively: say "Let me quickly check with
   my client and I'll call you right back." Then end the call politely.
4. Do not negotiate indefinitely. If nothing fits and they cannot hold
   a slot, politely end the call.

IF THE BUSINESS CANNOT HOLD WITHOUT IMMEDIATE CONFIRMATION:
Say: "I understand — I'll check with my client and call you right
back." Then end the call. Do not commit to any slot yourself.

SPECIAL INSTRUCTIONS FROM THE CLIENT TO RELAY TO THE BUSINESS:
{booking.special_instructions or "(none — just book the appointment)"}

GUARDRAILS:
- Never quote or agree to a price as final on the client's behalf.
- Never share sensitive personal, medical, or financial details beyond
  what is listed in the special instructions above.
- Never agree to cancellation policies or binding terms.
- If the business asks for information you don't have, say your client
  will follow up directly.
- If this is the wrong number or business, apologise and hang up.
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
            "output_audio_transcription": {},
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
            if booking.is_followup_call:
                kickoff = (
                    f"The call has connected. You are calling {booking.business_name} again "
                    f"as a followup. Open with ONE brief sentence reminding them you spoke "
                    f"before and that your client has confirmed the previously discussed time. "
                    f"Then STOP and wait for their response."
                )
            else:
                kickoff = (
                    f"The call has just connected. You are speaking with {booking.business_name}. "
                    f"Open with ONE brief, natural sentence — greet them and say you're calling "
                    f"to book a {booking.target_type or 'appointment'} for your client. "
                    f"Do NOT mention duration, deadline, or any other details yet. "
                    f"Say your one sentence, then STOP and wait for their reply."
                )

            # Use send_client_content (not send_realtime_input) to inject a
            # turn-based message that triggers the model to speak immediately.
            # send_realtime_input is for streaming live audio, not seeding turns.
            from google.genai import types as genai_types
            await session.send_client_content(
                turns=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=kickoff)],
                ),
                turn_complete=True,
            )

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
        try:
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
        except websockets.exceptions.ConnectionClosedError:
            # Caller hung up without sending a WebSocket close frame (abrupt hangup).
            # Treat this as a normal call end — don't propagate the exception.
            LOGGER.info(
                "Twilio WebSocket closed abruptly (no close frame) for call_sid=%s — treating as hangup",
                state.call_sid or "unknown",
            )
        except websockets.exceptions.ConnectionClosedOK:
            # Clean close — also fine, just return.
            pass

    async def _forward_gemini_to_twilio(self, websocket, session, state: BridgeState, matcher: SlotMatcher) -> None:
        gemini_buffer = ""

        async for response in session.receive():
            content = response.server_content
            tool_call = getattr(response, "tool_call", None)

            if tool_call and tool_call.function_calls:
                responses = []
                for fc in tool_call.function_calls:
                    if fc.name == "check_slot_fits":
                        try:
                            result = matcher.resolve_tool_call(dict(fc.args or {}))
                        except Exception as exc:
                            LOGGER.error("check_slot_fits raised unexpectedly: %s", exc)
                            result = {"fits": False, "reason": f"internal_error: {exc}"}
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
