#!/usr/bin/env python3
"""Environment configuration for voxlayer. No BookAppt/Carecaller-specific vars.

Everything is read lazily via getenv() at call time (not at import time) so
that a host application can set os.environ before importing/using voxlayer
functions, or load its own .env before importing this module.
"""

from __future__ import annotations

import os


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


class Config:
    # --- Twilio ---
    TWILIO_ACCOUNT_SID = "TWILIO_ACCOUNT_SID"
    TWILIO_AUTH_TOKEN = "TWILIO_AUTH_TOKEN"
    CALL_FROM_NUMBER = "VOXLAYER_CALL_FROM_NUMBER"
    RECORD_CALLS = "VOXLAYER_RECORD_CALLS"

    # --- Webhook / bridge wiring ---
    WEBHOOK_BASE_URL = "VOXLAYER_WEBHOOK_BASE_URL"
    OUTBOUND_TWIML_URL = "VOXLAYER_OUTBOUND_TWIML_URL"
    RECORDING_STATUS_CALLBACK_URL = "VOXLAYER_RECORDING_STATUS_CALLBACK_URL"
    MEDIA_STREAM_URL = "VOXLAYER_MEDIA_STREAM_URL"
    BRIDGE_HOST = "VOXLAYER_BRIDGE_HOST"
    BRIDGE_PORT = "VOXLAYER_BRIDGE_PORT"
    BRIDGE_PATH = "VOXLAYER_BRIDGE_PATH"
    HOST = "VOXLAYER_HOST"
    PORT = "VOXLAYER_PORT"

    # --- Gemini ---
    GEMINI_API_KEY = "GEMINI_API_KEY"
    GEMINI_LIVE_MODEL = "VOXLAYER_GEMINI_LIVE_MODEL"
    GEMINI_VOICE_NAME = "VOXLAYER_GEMINI_VOICE_NAME"
    NORMALIZER_MODEL = "VOXLAYER_NORMALIZER_MODEL"
    EXTRACTOR_MODEL = "VOXLAYER_EXTRACTOR_MODEL"
    NORMALIZER_API_KEY = "VOXLAYER_NORMALIZER_GEMINI_API_KEY"  # optional dedicated key

    # --- Whisper ---
    WHISPER_MODEL = "VOXLAYER_WHISPER_MODEL"
    WHISPER_DEVICE = "VOXLAYER_WHISPER_DEVICE"
    WHISPER_COMPUTE_TYPE = "VOXLAYER_WHISPER_COMPUTE_TYPE"
    WHISPER_LANGUAGE = "VOXLAYER_WHISPER_LANGUAGE"
    WHISPER_FALLBACK_TO_CPU = "VOXLAYER_WHISPER_FALLBACK_TO_CPU"

    # --- Storage directories (all relative to VOXLAYER_DATA_DIR unless absolute) ---
    DATA_DIR = "VOXLAYER_DATA_DIR"
    RECORDINGS_DIR = "VOXLAYER_RECORDINGS_DIR"
    WHISPER_TRANSCRIPT_DIR = "VOXLAYER_WHISPER_TRANSCRIPT_DIR"
    CONVERSATION_DIR = "VOXLAYER_CONVERSATION_DIR"
    FINAL_TRANSCRIPT_DIR = "VOXLAYER_FINAL_TRANSCRIPT_DIR"
    NORMALIZED_TRANSCRIPT_DIR = "VOXLAYER_NORMALIZED_TRANSCRIPT_DIR"
    RESULTS_DIR = "VOXLAYER_RESULTS_DIR"
    NEGOTIATION_STATE_DIR = "VOXLAYER_NEGOTIATION_STATE_DIR"

    # --- Automation toggles ---
    AUTO_TRANSCRIBE = "VOXLAYER_AUTO_TRANSCRIBE"
    AUTO_BUILD_FINAL_TRANSCRIPT = "VOXLAYER_AUTO_BUILD_FINAL_TRANSCRIPT"
    AUTO_NORMALIZE = "VOXLAYER_AUTO_NORMALIZE"
    AUTO_EXTRACT = "VOXLAYER_AUTO_EXTRACT"
    AUTO_UPDATE_RESULTS = "VOXLAYER_AUTO_UPDATE_RESULTS"

    @staticmethod
    def get(key: str, default: str = "") -> str:
        return os.getenv(key, default)

    @staticmethod
    def get_bool(key: str, default: bool = True) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return default
        return _truthy(raw)

    @staticmethod
    def get_int(key: str, default: int) -> int:
        raw = os.getenv(key)
        if raw is None or not raw.strip():
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @classmethod
    def data_path(cls, subdir_key: str, default_subdir: str) -> str:
        """Resolve a data subdirectory under DATA_DIR (default 'voxlayer_data')."""
        base = os.getenv(cls.DATA_DIR, "voxlayer_data")
        sub = os.getenv(subdir_key, default_subdir)
        if os.path.isabs(sub):
            return sub
        return os.path.join(base, sub)
