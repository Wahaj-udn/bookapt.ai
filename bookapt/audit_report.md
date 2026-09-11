# voxlayer — Full Self-Sufficiency Audit

> Python 3.11.0 · venv with all requirements installed · 15 automated checks run

---

## Summary

| Category | Count |
|---|---|
| ✅ Checks passed (logic correct) | 12 |
| 🐛 Real bugs to fix | 2 |
| ⚠️ Design gaps (not breaking, but misleading) | 2 |
| 🧹 Minor / cleanup | 1 |

---

## 🐛 Bug 1 — `audioop` removed in Python 3.13 · [`bridge.py`](file:///d:/Wahaj/Projects/Hackathons/MRCET/bookapt/bridge.py#L27)

```
DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
```

`bridge.py` does `import audioop` (line 27) for μ-law ↔ PCM conversion. This stdlib module was **removed in Python 3.13**. Currently safe on 3.11 but will hard-crash on upgrade.

**Fix:** Add `audioop-lts` to `requirements.txt` and add a conditional import:
```python
try:
    import audioop
except ModuleNotFoundError:          # Python 3.13+
    import audioop_lts as audioop    # pip install audioop-lts
```

---

## 🐛 Bug 2 — `booking_id` missing from recording status callback URL · [`caller.py`](file:///d:/Wahaj/Projects/Hackathons/MRCET/bookapt/caller.py#L76)

`_build_twiml_url()` correctly appends `booking_id` to the TwiML URL (so `bridge.py` and `/voice/outbound` both get it). However, `recording_status_callback` is taken from env as-is and is **never augmented with `booking_id`**:

```python
# caller.py line 76-79
callback = Config.get(Config.RECORDING_STATUS_CALLBACK_URL)
if callback:
    kwargs["recording_status_callback"] = callback   # ← no booking_id appended
```

`server.py`'s `/voice/recording` handler tries to recover it:
```python
booking_id = (request.args.get("booking_id") or event.get("booking_id") or "").strip()
```

Twilio's recording callback form body does **not** include `booking_id` (it's not a Twilio field). So `booking_id` will always be `""` in the recording callback, making `result_store.record_call_result()` write a result under an empty booking ID — **the post-call pipeline silently runs but stores results under the wrong key**.

**Fix:** Augment the recording callback URL the same way TwiML URL is augmented:
```python
# in _place(), after building the callback URL:
if callback:
    kwargs["recording_status_callback"] = _append_query(callback, {"booking_id": booking.booking_id})
```

---

## ⚠️ Design Gap 1 — `VOICEMAIL` missing from `TERMINAL_STATES` · [`negotiation_state.py`](file:///d:/Wahaj/Projects/Hackathons/MRCET/bookapt/negotiation_state.py#L50)

```python
TERMINAL_STATES = {BOOKED, NO_AVAILABILITY, DECLINED, WRONG_NUMBER, ESCALATE_TO_HUMAN}
# ↑ VOICEMAIL is absent
```

`VOICEMAIL` is a valid `NegotiationOutcome` and maps to `NegotiationState.VOICEMAIL` in `mark_resolved()`, but it's not listed in `TERMINAL_STATES`. `TERMINAL_STATES` is currently only informational (nothing checks it at runtime), so this is not a crash bug — but it's inconsistent and will mislead any future code that uses `TERMINAL_STATES` to guard against re-calling a resolved booking.

**Fix:** Add `VOICEMAIL` to `TERMINAL_STATES`.

---

## ⚠️ Design Gap 2 — `CallResult` is documented as the outbound contract but is never instantiated · [`models.py`](file:///d:/Wahaj/Projects/Hackathons/MRCET/bookapt/models.py#L111), [`__init__.py`](file:///d:/Wahaj/Projects/Hackathons/MRCET/bookapt/__init__.py#L20)

The README and `__init__.py` both describe `models.CallResult` as the outbound contract:
> `Outbound -> models.CallResult`

In practice, the entire pipeline (`extractor.py` → `result_store.py`) produces and stores **plain `dict`** objects, never a `CallResult` instance. `CallResult` is exported but is dead code within the package — it's a schema without a constructor.

**Options:**
- Convert `result_store.record_call_result()` to construct and return a `CallResult`; OR
- Update the README/`__init__.py` docstring to say the outbound is a `dict` (less clean but honest)

---

## 🧹 Minor — Unused import `SlotWindow` in [`negotiation_state.py`](file:///d:/Wahaj/Projects/Hackathons/MRCET/bookapt/negotiation_state.py#L36)

```python
from .models import NegotiationOutcome, SlotWindow   # SlotWindow never used
```

Not a runtime error, but clutters the import and could confuse a reader into thinking the state machine directly handles slot windows. Safe to remove.

---

## ✅ Checks That Passed

| # | What was verified | Result |
|---|---|---|
| 1 | All 10 modules import cleanly (no missing deps) | ✅ |
| 2 | `NegotiationOutcome.values()` == `normalizer.OUTCOME_LABELS` | ✅ Exact match |
| 3 | `SlotMatcher.tool_schema()` required fields ⊆ properties | ✅ |
| 4 | SlotMatcher accept/reject logic (inside window, past max_date, no match) | ✅ |
| 5 | `BookingRequest` JSON serialization round-trip | ✅ |
| 6 | `build_mission_prompt()` contains `check_slot_fits`, business name, duration | ✅ |
| 7 | Normalizer post-processing: consecutive speaker merge, outcome line guarding | ✅ |
| 8 | Extractor `_read_outcome_and_body()` correctly separates outcome from body | ✅ |
| 9 | `negotiation_state` full state machine: NEW→IN_CALL→HELD→BOOKED | ✅ |
| 10 | `mark_resolved()` terminal_map covers all 7 `NegotiationOutcome` values | ✅ |
| 11 | Config keys consistent across all files | ✅ |
| 12 | `extractor.py` prompt fields match `result_store.py` consumption keys | ✅ |
