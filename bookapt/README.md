# voxlayer

A self-sufficient **call → record → transcribe → normalize → extract →
aggregate** layer for BookAppt.ai, extracted and generalized from the
Carecaller repo. Drop this folder into the BookAppt.ai project as a
package; it has no dependency on the dashboard, calendar logic, or UI.

## What this replaces from Carecaller, 1:1

| Carecaller file                        | voxlayer equivalent      | What changed |
|-----------------------------------------|--------------------------|--------------|
| `call.py`                                | `caller.py`               | Generalized; added `place_followup_call()` for the negotiation callback; dropped CSV batch queue (one booking at a time, per your call) |
| `server.py`                              | `server.py`               | Generalized routes; `booking_id` flows through instead of `patient_name` |
| `gemini_bridge.py`                       | `bridge.py`               | Generic mission prompt built from `BookingRequest`; added `check_slot_fits` tool-calling; dropped healthcare disclaimers |
| `whisper_transcriber.py`                 | `transcriber.py`          | Logic unchanged, config renamed |
| `final_transcript_builder.py`            | `transcript_builder.py`   | Logic unchanged (already domain-agnostic), config renamed |
| `normalize_transcript_with_gemini.py`    | `normalizer.py`           | Outcome labels changed to negotiation taxonomy; dropped 14-question section |
| `extract_responses.py`                   | `extractor.py`            | **Rewritten** — no fixed question list, so this is an LLM extraction pass producing outcome/key-fields/summary instead of regex Q&A matching |
| `build_result_json.py`                   | `result_store.py`         | Keyed per-booking instead of one global file; drives `negotiation_state` transitions |
| *(none — new for BookAppt)*              | `negotiation_state.py`    | Multi-call state machine for the hold/approve/callback flow |
| *(none — new for BookAppt)*              | `slot_matcher.py`         | Deterministic accept/reject tool, called live during the conversation |
| *(none — new for BookAppt)*              | `models.py`               | The shared data contract (see below) |

`call_csv.py` / `csv_call_queue.py` were **not** ported — confirmed out of
scope since BookAppt negotiates one booking at a time, not a batch queue.

## The contract (the seam between this layer and the rest of BookAppt.ai)

This is the only thing the other layer (dashboard + calendar reader +
negotiation-payload builder) needs to know about:

```
Inbound:   models.BookingRequest   — everything voxlayer needs to run a call
Outbound:  per-booking JSON in     — result_store.get_booking_history(booking_id)
           results/<booking_id>.json
```

`BookingRequest` fields the calendar/dashboard layer must supply:

- `booking_id`, `business_name`, `business_phone`, `target_type`
- `max_date` — hard ceiling, not a soft preference
- `required_duration_minutes`
- `fitting_slots` — **precomputed** list of calendar windows that already
  satisfy the duration. voxlayer never talks to Google Calendar; it only
  does deterministic interval-matching against this list during the call
  (see `slot_matcher.py`). This was the explicit design decision from the
  planning discussion: precompute the availability, check live via a
  tool-call rather than either pure prompt-reasoning or a live Calendar
  API round-trip mid-call.
- `special_instructions`, `user_display_name`
- (`is_followup_call` / `prior_offer_summary` are filled automatically by
  `caller.place_followup_call()` — the host app doesn't set these itself)

Everything downstream of that (calling, negotiating, recording,
transcribing, classifying outcome, summarizing) is voxlayer's job.

## Outcome taxonomy (final, as agreed)

```
booked | pending_user_approval | no_availability | declined |
wrong_number | voicemail | escalate_to_human
```

Defined once in `models.NegotiationOutcome`; both `normalizer.py` and
`extractor.py` import it so the label set can't drift out of sync.

## Negotiation flow, mapped to code

1. `caller.place_call(booking)` — first call, stamps `negotiation_state` to `IN_CALL`
2. `bridge.py` conducts the conversation live. Whenever the business proposes
   a time, the model is instructed to call the `check_slot_fits` tool
   (`slot_matcher.py`) — a deterministic, non-LLM accept/reject check
   against `fitting_slots` + `max_date`. **The bridge never decides the
   call's outcome itself** — it just conducts the conversation and logs it.
3. After the call, `server.py`'s post-call pipeline runs automatically:
   record → transcribe → align → normalize → extract → `result_store.record_call_result()`
4. `result_store` reads the extracted outcome and advances
   `negotiation_state`:
   - `pending_user_approval` → stamps the held offer window, dashboard's
     `negotiation_state.list_pending_approvals()` will now surface it
   - anything else → terminal state, done
5. Dashboard shows the alert (from step 4) → user clicks approve →
   host app calls `negotiation_state.approve_held_offer(booking_id)` then
   `caller.place_followup_call(booking)` — this automatically injects
   "you spoke with them before, they offered X, client approved" into the
   followup call's mission prompt.

No timeout logic anywhere in this chain, per spec — `HELD_PENDING_APPROVAL`
just sits until the host app calls `approve_held_offer()`.

## Running it (once wired into the host app)

Same two-tunnel shape as Carecaller:

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in secrets + public URLs

# Terminal 1 — bridge (needs a booking_loader from the host app; see
# example_integration.py's `example_booking_loader`)
python -c "from voxlayer import bridge; bridge.main(booking_loader=my_loader)"

# Terminal 2 — expose bridge publicly, set VOXLAYER_MEDIA_STREAM_URL to it

# Terminal 3 — webhook server
python -m voxlayer.server

# Terminal 4 — expose webhook publicly, set VOXLAYER_WEBHOOK_BASE_URL /
# VOXLAYER_OUTBOUND_TWIML_URL / VOXLAYER_RECORDING_STATUS_CALLBACK_URL

# Then, from the host app:
from voxlayer import caller
caller.place_call(booking)
```

See `example_integration.py` for the five concrete points the host app
needs to hook: starting a booking, polling alerts, approving a hold,
rendering history, and supplying `booking_loader`.

## Gaps / things NOT handled here (by design, per earlier discussion)

- **No calendar access anywhere in this package.** The calendar layer
  must precompute `fitting_slots` before calling `caller.place_call()`.
  Calendar race conditions (slot taken between precompute and call) were
  explicitly deferred as an accepted edge case.
- **No WhatsApp channel.** Phase 2, per your call.
- **No negotiation round-capping.** The mission prompt asks the model to
  not spiral indefinitely, but there's no hard code-enforced cap — you
  said the user self-regulates by not approving further rounds.
- **No approval timeout.** `HELD_PENDING_APPROVAL` is not time-boxed.
- **`booking_loader` is a stub the host app must implement** (see
  `example_integration.py`) — voxlayer intentionally doesn't know how or
  where bookings are stored (DB, memory, file), so `bridge.py` takes this
  as a plain callable rather than importing any storage code.
- **Business-won't-hold fallback is a fixed prompt line**, not a separate
  code path — matches what you described (hardcoded line is enough for v1).
- **Stale-hold recovery on the followup call is not special-cased** — if
  the business already gave the slot away, the mission prompt's normal
  negotiation logic just runs again from scratch on that call, per your
  answer that this doesn't need a distinct script.

## Testing without a live Twilio number

`transcript_builder.py`, `normalizer.py`, `extractor.py`, and
`result_store.py` all operate on plain files and can be exercised directly
against a hand-written `conversation/*.txt` + `whisper_transcript/*.txt`
pair, exactly like Carecaller's existing test suite
(`test_final_transcript_builder.py`, `test_normalize_transcript.py`) did —
port those tests over and swap in negotiation-flavored fixture transcripts
rather than health check-in ones.
