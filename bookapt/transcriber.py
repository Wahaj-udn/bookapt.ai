#!/usr/bin/env python3
"""Local Faster-Whisper transcription — generalized from Carecaller's
whisper_transcriber.py. Logic is unchanged; only naming/config are
generalized (no BookAppt/Carecaller-specific env vars)."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

from faster_whisper import WhisperModel

from .config import Config

_DLL_DIR_HANDLES: list[object] = []


@lru_cache(maxsize=4)
def _get_model(model_name: str, device: str, compute_type: str) -> WhisperModel:
    return WhisperModel(model_name, device=device, compute_type=compute_type)


@lru_cache(maxsize=1)
def _configure_windows_cuda_dll_search_paths() -> None:
    if os.name != "nt":
        return
    site_packages = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    candidate_bins = [
        site_packages / "cublas" / "bin",
        site_packages / "cuda_nvrtc" / "bin",
        site_packages / "cuda_runtime" / "bin",
        site_packages / "cudnn" / "bin",
    ]
    for bin_dir in candidate_bins:
        if not bin_dir.exists():
            continue
        current_path = os.environ.get("PATH", "")
        bin_dir_str = str(bin_dir)
        if bin_dir_str.lower() not in current_path.lower():
            os.environ["PATH"] = f"{bin_dir_str};{current_path}" if current_path else bin_dir_str
        try:
            _DLL_DIR_HANDLES.append(os.add_dll_directory(bin_dir_str))
        except (AttributeError, FileNotFoundError, OSError):
            pass


def _format_timestamp(seconds: float) -> str:
    total_ms = int(max(0.0, seconds) * 1000)
    hours = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    minutes = rem // 60_000
    rem = rem % 60_000
    secs = rem // 1000
    ms = rem % 1000
    return f"{hours:02}:{minutes:02}:{secs:02}.{ms:03}"


def transcript_path_for_recording(recording_file: Path, transcript_dir: Path) -> Path:
    return transcript_dir / f"{recording_file.stem}.txt"


def transcribe(recording_file: Path) -> Path:
    """Transcribe one recording using env-configured Whisper settings.
    Returns the output transcript path (idempotent: skips if it exists)."""
    transcript_dir = Path(Config.data_path(Config.WHISPER_TRANSCRIPT_DIR, "whisper_transcript")).resolve()
    model_name = Config.get(Config.WHISPER_MODEL, "small")
    device = Config.get(Config.WHISPER_DEVICE, "cuda")
    compute_type = Config.get(Config.WHISPER_COMPUTE_TYPE, "float16")
    language = Config.get(Config.WHISPER_LANGUAGE, "en") or None
    fallback_to_cpu = Config.get_bool(Config.WHISPER_FALLBACK_TO_CPU, False)

    recording_file = recording_file.resolve()
    transcript_dir.mkdir(parents=True, exist_ok=True)
    if not recording_file.exists():
        raise FileNotFoundError(f"Recording not found: {recording_file}")

    output_file = transcript_path_for_recording(recording_file, transcript_dir)
    if output_file.exists():
        return output_file

    _configure_windows_cuda_dll_search_paths()

    try:
        model = _get_model(model_name, device, compute_type)
        segments, info = model.transcribe(str(recording_file), language=language, beam_size=5, vad_filter=True)
    except Exception as exc:
        if not (fallback_to_cpu and device.lower() == "cuda"):
            raise
        error_text = str(exc).lower()
        if not any(tok in error_text for tok in ("cublas", "cudnn", "cuda", "dll")):
            raise
        cpu_model = _get_model(model_name, "cpu", "int8")
        segments, info = cpu_model.transcribe(str(recording_file), language=language, beam_size=5, vad_filter=True)

    lines: list[str] = [
        f"source={recording_file.name}",
        f"language={getattr(info, 'language', 'unknown')}",
        f"duration={getattr(info, 'duration', 'unknown')}",
        "",
    ]
    for segment in segments:
        start = _format_timestamp(float(segment.start))
        end = _format_timestamp(float(segment.end))
        text = (segment.text or "").strip()
        if text:
            lines.append(f"[{start} -> {end}] {text}")
    if len(lines) == 4:
        lines.append("[no speech recognized]")

    output_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output_file
