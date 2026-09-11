#!/usr/bin/env python3
"""Align Whisper segments with the live conversation log to produce a
speaker-labeled final transcript. Logic ported near-verbatim from
Carecaller's final_transcript_builder.py — this part is domain-agnostic
already, so only the entry point (`build`) and path config are new."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Optional

from .config import Config

WHISPER_SEGMENT_RE = re.compile(
    r"^\[(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s*->\s*(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})\]\s*(?P<text>.+)$"
)
CONVERSATION_LINE_RE = re.compile(
    r"^(?:\[(?P<start>[\d.]+)-(?P<end>[\d.]+)\]\s*)?(?P<speaker>agent|user)>(?P<text>.+)$"
)
CALL_SID_RE = re.compile(r"CA[A-Za-z0-9]{32}")

AGENT_FILLER_PHRASES = {
    "ok", "okay", "got it", "thanks", "thank you", "great", "no problem",
    "sounds good", "perfect", "understood",
}


@dataclass
class WhisperSegment:
    start_s: float
    end_s: float
    text: str


@dataclass
class ConversationTurn:
    speaker: str
    text: str
    start_s: Optional[float] = None
    end_s: Optional[float] = None


@dataclass
class FinalLine:
    start_s: float
    end_s: float
    speaker: str
    text: str


def _parse_hhmmss_ms(text: str) -> float:
    hh, mm, rest = text.split(":")
    ss, ms = rest.split(".")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def _format_compact_seconds(seconds: float) -> str:
    return f"{max(0.0, seconds):.3f}".rstrip("0").rstrip(".") or "0"


def _normalize(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _is_agent_filler_phrase(text: str) -> bool:
    norm = _normalize(text)
    if not norm or len(norm.split()) > 6:
        return False
    return norm in AGENT_FILLER_PHRASES


def parse_whisper_transcript(path: Path) -> tuple[list[str], list[WhisperSegment]]:
    header: list[str] = []
    segments: list[WhisperSegment] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = WHISPER_SEGMENT_RE.match(stripped)
        if not match:
            if stripped or not segments:
                header.append(line)
            continue
        segments.append(
            WhisperSegment(
                start_s=_parse_hhmmss_ms(match.group("start")),
                end_s=_parse_hhmmss_ms(match.group("end")),
                text=match.group("text").strip(),
            )
        )
    return header, segments


def parse_conversation_file(path: Path) -> tuple[Optional[str], list[ConversationTurn]]:
    call_sid: Optional[str] = None
    turns: list[ConversationTurn] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("# call_sid="):
            call_sid = stripped.split("=", 1)[-1].strip() or call_sid
            continue
        match = CONVERSATION_LINE_RE.match(stripped)
        if not match:
            continue
        start_s = float(match.group("start")) if match.group("start") is not None else None
        end_s = float(match.group("end")) if match.group("end") is not None else None
        turns.append(
            ConversationTurn(speaker=match.group("speaker"), text=match.group("text").strip(), start_s=start_s, end_s=end_s)
        )
    return call_sid, turns


def find_conversation_for_call(call_sid: str, conversation_dir: Path) -> Optional[Path]:
    candidates = sorted(conversation_dir.glob(f"*_{call_sid}.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _time_overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    left, right = max(a_start, b_start), min(a_end, b_end)
    if right <= left:
        return 0.0
    return (right - left) / max(a_end - a_start, 1e-6)


def _choose_speaker(seg: WhisperSegment, turns: list[ConversationTurn], last_index: int, min_score: float, time_offset_s: float) -> tuple[str, int]:
    if _is_agent_filler_phrase(seg.text):
        return "agent", last_index

    agent_timed = [t for t in turns if t.speaker == "agent" and t.start_s is not None and t.end_s is not None]
    if agent_timed:
        for turn in agent_timed:
            start, end = turn.start_s + time_offset_s, turn.end_s + time_offset_s
            overlap = _time_overlap_ratio(seg.start_s, seg.end_s, start, end)
            mid = (seg.start_s + seg.end_s) / 2.0
            if overlap >= 0.25 or (start <= mid <= end):
                return "agent", last_index
        return "user", last_index

    norm_seg = _normalize(seg.text)
    start_i, end_i = max(0, last_index - 2), min(len(turns), last_index + 12) if turns else 0
    best_i, best_score = -1, 0.0
    for i in range(start_i, end_i):
        turn = turns[i]
        if not turn.text:
            continue
        ratio = SequenceMatcher(None, norm_seg, _normalize(turn.text)).ratio()
        if turn.start_s is not None and turn.end_s is not None:
            ratio += 0.35 * _time_overlap_ratio(seg.start_s, seg.end_s, turn.start_s, turn.end_s)
            mid = (seg.start_s + seg.end_s) / 2.0
            if turn.start_s <= mid <= turn.end_s:
                ratio += 0.2
        if ratio > best_score:
            best_score, best_i = ratio, i
    if best_i >= 0 and best_score >= min_score:
        return turns[best_i].speaker, best_i
    for turn in turns:
        if turn.speaker != "agent" or turn.start_s is None or turn.end_s is None:
            continue
        if _time_overlap_ratio(seg.start_s, seg.end_s, turn.start_s, turn.end_s) >= 0.25:
            return "agent", last_index
    return "user", last_index


def merge_consecutive_same_speaker(lines: list[FinalLine]) -> list[FinalLine]:
    if not lines:
        return []
    merged: list[FinalLine] = [lines[0]]
    for line in lines[1:]:
        prev = merged[-1]
        if line.speaker == prev.speaker:
            prev.end_s = max(prev.end_s, line.end_s)
            prev.text = f"{prev.text} {line.text}".strip()
        else:
            merged.append(line)
    return merged


def estimate_time_offset_seconds(segments: list[WhisperSegment], turns: list[ConversationTurn]) -> float:
    agent_turns = [t for t in turns if t.speaker == "agent" and t.start_s is not None and t.text]
    if not agent_turns or not segments:
        return 0.0
    deltas: list[float] = []
    for turn in agent_turns:
        turn_norm = _normalize(turn.text)
        best_ratio, best_start = 0.0, None
        for seg in segments:
            ratio = SequenceMatcher(None, _normalize(seg.text), turn_norm).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, seg.start_s
        if best_start is not None and best_ratio >= 0.55:
            deltas.append(best_start - float(turn.start_s))
    return float(median(deltas)) if deltas else 0.0


def build_final_transcript(whisper_path: Path, conversation_path: Path, output_dir: Path, min_score: float = 0.42) -> Path:
    header, segments = parse_whisper_transcript(whisper_path)
    _, turns = parse_conversation_file(conversation_path)
    time_offset_s = estimate_time_offset_seconds(segments, turns)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / whisper_path.name

    lines: list[str] = [h for h in header if h.strip()]
    lines.append(f"conversation_source={conversation_path.name}")
    lines.append("")

    last_idx = 0
    final_lines: list[FinalLine] = []
    for seg in segments:
        speaker, last_idx = _choose_speaker(seg, turns, last_idx, min_score=min_score, time_offset_s=time_offset_s)
        final_lines.append(FinalLine(start_s=seg.start_s, end_s=seg.end_s, speaker=speaker, text=seg.text))

    for item in merge_consecutive_same_speaker(final_lines):
        lines.append(f"[{_format_compact_seconds(item.start_s)}-{_format_compact_seconds(item.end_s)}] {item.speaker}> {item.text}")

    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output_path


def build(whisper_file: Path, call_sid: str, min_score: float = 0.42) -> Optional[Path]:
    """Entry point used by server.py's post-call pipeline."""
    conversation_dir = Path(Config.data_path(Config.CONVERSATION_DIR, "conversation")).resolve()
    output_dir = Path(Config.data_path(Config.FINAL_TRANSCRIPT_DIR, "final_transcript")).resolve()

    conversation_file = find_conversation_for_call(call_sid, conversation_dir)
    if not conversation_file:
        return None
    return build_final_transcript(whisper_path=whisper_file, conversation_path=conversation_file, output_dir=output_dir, min_score=min_score)
