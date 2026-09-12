#!/usr/bin/env python3
"""Normalize noisy negotiation-call transcripts using Gemini text generation.

Generalized from Carecaller's normalize_transcript_with_gemini.py. The
structural contract is unchanged (first line `outcome=<label>`, then strict
`[AGENT]:` / `[USER]:` lines) but:
- the outcome label set matches models.NegotiationOutcome instead of the
  healthcare labels
- the 14-canonical-questions section is gone; there's no fixed question
  list for a negotiation call
- cleaning rules are generalized to focus on dates/times/prices instead of
  weight/height/medical fields
"""

from __future__ import annotations

import re
from pathlib import Path

from dotenv import load_dotenv
from google import genai

from .config import Config
from .models import NegotiationOutcome

load_dotenv()

OUTCOME_LABELS = set(NegotiationOutcome.values())
DEFAULT_OUTCOME = NegotiationOutcome.ESCALATE_TO_HUMAN.value


def _get_api_key() -> str:
    dedicated = Config.get(Config.NORMALIZER_API_KEY)
    return dedicated or Config.get(Config.GEMINI_API_KEY)


def build_prompt(raw_transcript: str) -> str:
    return f"""You are a transcript normalization engine for an AI appointment-booking
assistant that calls businesses on behalf of a client.

CONTEXT:
The input is a raw conversation transcript generated from speech-to-text.
It contains timestamps, inconsistent speaker labels, and transcription noise.

The output must be a clean conversation using ONLY:
- [AGENT]: for the AI calling assistant
- [USER]: for the person at the business who answered

----------------------------------------
OUTCOME CLASSIFICATION (FIRST LINE, STRICT)
----------------------------------------

You MUST output the FIRST line exactly in this format:
outcome=<label>

Allowed labels:
- booked                 (a specific date/time was proposed by the business,
                           confirmed as fitting, and clearly accepted by the agent)
- pending_user_approval   (the agent asked the business to hold a specific time
                           and said it would call back with confirmation, without
                           accepting it outright)
- no_availability         (the business had nothing that could ever satisfy the
                           agent's constraints, and no hold was arranged)
- declined                (the business explicitly refused to schedule or
                           continue, for reasons unrelated to wrong number)
- wrong_number            (the person who answered indicated this is not the
                           right business/number)
- voicemail               (no real back-and-forth; call reached voicemail or
                           an automated system with no live person)
- escalate_to_human       (anything ambiguous, confusing, or not clearly
                           matching the above — this is also the safe default)

Decision priority if multiple seem possible:
wrong_number > voicemail > declined > booked > pending_user_approval > no_availability > escalate_to_human

If uncertain, choose: escalate_to_human

----------------------------------------
STRICT RULES
----------------------------------------

- Do NOT add new information or hallucinate missing content.
- Do NOT change the meaning of what either party said.
- Only reorganize, clean, and correct structure.
- Do NOT rewrite the business person's (USER) words/content.
- You may correct AGENT wording only for obvious transcription noise (repeated
  words, cut-off phrases) — never change what the agent actually offered,
  accepted, or asked.

----------------------------------------
CLEANING RULES
----------------------------------------

1. Remove system-level noise (e.g. "you have a trial account", "connecting you...").
2. Fix speaker labels: the AGENT proposes/confirms/asks questions; the USER
   (business side) answers, proposes times, states prices/policies. If a
   line contains both, split it correctly.
3. Split lines when multiple distinct statements or a question + answer are
   merged into one line.
4. Merge consecutive lines from the same speaker into one line.
5. Normalize spoken dates, times, and prices to a consistent written form
   for USER lines only (e.g. "next Tuesday at three" -> "next Tuesday at
   3:00 PM", "fifty bucks" -> "$50") — never invent a value that wasn't
   stated, and never touch AGENT lines this way.
6. Preserve natural conversational tone; do not summarize.

----------------------------------------
OUTPUT FORMAT (STRICT)
----------------------------------------

Return a SINGLE STRING exactly like this:

"outcome=booked
[AGENT]: ...
[USER]: ...
[AGENT]: ...
[USER]: ..."

- First line MUST be outcome=<label>
- No timestamps, no extra commentary, no JSON, no explanation.

----------------------------------------
INPUT:
{raw_transcript}
"""


def sanitize_model_output(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def ensure_outcome_first_line(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return f"outcome={DEFAULT_OUTCOME}"

    outcome_pattern = re.compile(r"^outcome\s*=\s*([a-z_]+)\s*$", re.IGNORECASE)
    outcome_line = None
    remaining: list[str] = []
    for ln in lines:
        m = outcome_pattern.match(ln.strip())
        if m and outcome_line is None:
            label = m.group(1).lower()
            if label in OUTCOME_LABELS:
                outcome_line = f"outcome={label}"
                continue
        remaining.append(ln)

    if outcome_line is None:
        outcome_line = f"outcome={DEFAULT_OUTCOME}"
    return "\n".join([outcome_line, *remaining]).strip()


def merge_consecutive_speaker_tags(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pattern = re.compile(r"^\[(AGENT|USER)\]\s*:\s*(.*)$", re.IGNORECASE)
    merged: list[str] = []
    current_speaker: str | None = None
    current_text = ""

    def flush() -> None:
        nonlocal current_speaker, current_text
        if current_speaker is not None:
            merged.append(f"[{current_speaker}]: {current_text.strip()}")
            current_speaker, current_text = None, ""

    for line in lines:
        m = pattern.match(line)
        if m:
            speaker, content = m.group(1).upper(), m.group(2).strip()
            if current_speaker == speaker:
                current_text = f"{current_text} {content}".strip()
            else:
                flush()
                current_speaker, current_text = speaker, content
            continue
        if current_speaker is not None:
            current_text = f"{current_text} {line}".strip()
        else:
            merged.append(line)
    flush()
    return "\n".join(merged).strip()


def normalize_transcript(input_file: Path, output_file: Path, model: str) -> Path:
    from .gemini_retry import call_with_retry

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing normalizer API key. Set VOXLAYER_NORMALIZER_GEMINI_API_KEY or GEMINI_API_KEY."
        )

    raw_text = input_file.read_text(encoding="utf-8")
    prompt = build_prompt(raw_text)

    client = genai.Client(api_key=api_key)

    # Layer 3: fallback model if primary keeps 503-ing
    fallback_model = Config.get("VOXLAYER_NORMALIZER_FALLBACK_MODEL", "").strip()

    def _call_primary() -> str:
        resp = client.models.generate_content(model=model, contents=prompt)
        return resp.text or ""

    def _call_fallback() -> str:
        resp = client.models.generate_content(model=fallback_model, contents=prompt)
        return resp.text or ""

    raw_output = call_with_retry(
        _call_primary,
        label=f"normalizer/{model}",
        fallback_fn=_call_fallback if fallback_model and fallback_model != model else None,
    )

    output_text = sanitize_model_output(raw_output)
    output_text = ensure_outcome_first_line(output_text)
    output_text = merge_consecutive_speaker_tags(output_text)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(output_text + "\n", encoding="utf-8")
    return output_file



def normalize(input_file: Path) -> Path:
    """Entry point used by server.py's post-call pipeline."""
    output_dir = Path(Config.data_path(Config.NORMALIZED_TRANSCRIPT_DIR, "normalized_transcript")).resolve()
    model = Config.get(Config.NORMALIZER_MODEL, "gemini-2.5-flash")
    output_file = output_dir / f"{input_file.stem}.normalized.txt"
    return normalize_transcript(input_file=input_file, output_file=output_file, model=model)
