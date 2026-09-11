# BookAppt.ai — Full Implementation Plan

## What's Already Done

The `bookapt/` folder **is** the `voxlayer` calling engine — fully built and verified:
- Outbound call placement (Twilio) → `caller.py`
- Live Gemini negotiation bridge → `bridge.py`
- Webhook server → `server.py`
- Slot-fit checking tool → `slot_matcher.py`
- Per-booking negotiation state machine → `negotiation_state.py`
- Whisper transcription → `transcriber.py`
- Transcript alignment → `transcript_builder.py`
- Gemini normalization + extraction → `normalizer.py`, `extractor.py`
- Result persistence → `result_store.py`
- Shared data contract → `models.py`

Everything below is the **host application** that wraps it.

---

## Target Directory Layout

```
d:\Wahaj\Projects\Hackathons\MRCET\
├── bookapt/                    ← voxlayer engine (done)
└── app/                        ← host application (to build)
    ├── run.py                  ← entrypoint
    ├── app.py                  ← Flask app factory
    ├── config.py               ← host-app config (DB path, Google creds, etc.)
    ├── db.py                   ← SQLite schema + helpers
    ├── booking_loader.py       ← the callable voxlayer needs
    ├── calendar_reader.py      ← Google Calendar OAuth + free-block finder
    ├── routes/
    │   ├── __init__.py
    │   ├── dashboard.py        ← GET  /
    │   ├── records.py          ← saved business records CRUD
    │   ├── bookings.py         ← create booking, view detail
    │   ├── calendar_auth.py    ← /calendar/auth, /calendar/callback
    │   └── alerts.py           ← list pending holds, approve/reject
    ├── templates/
    │   ├── base.html
    │   ├── dashboard.html      ← history table + alert badge
    │   ├── new_booking.html    ← the input form (dual entry paths)
    │   └── booking_detail.html ← single-booking transcript + summary
    └── static/
        ├── css/style.css
        └── js/main.js
```

> [!IMPORTANT]
> The `app/` directory **imports from** `bookapt/` as a Python package. Both live under `d:\Wahaj\Projects\Hackathons\MRCET\`, so `import bookapt` works from `app/` without installing it.

---

## Build Sequence — 8 Phases

---

### Phase 1 — Host App Scaffold + DB

**Goal:** A runnable Flask app with a SQLite database, no UI yet.

#### [NEW] `app/config.py`
- `SECRET_KEY`, `DB_PATH`, Google OAuth client credentials, `DATA_DIR` pointer to voxlayer's data folder
- Reads from a `.env` file (same pattern as voxlayer)

#### [NEW] `app/db.py`
Two tables:

```sql
-- Saved business records (reusable across bookings)
CREATE TABLE business_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,          -- user-given name e.g. "Dr. Smith"
    target_type TEXT NOT NULL,          -- "doctor" | "salon" | "mechanic" etc.
    phone       TEXT NOT NULL UNIQUE,   -- E.164; the join key
    created_at  TEXT NOT NULL
);

-- One row per booking attempt
CREATE TABLE bookings (
    id                       TEXT PRIMARY KEY,   -- booking_id (UUID)
    business_record_id       INTEGER REFERENCES business_records(id),
    preferred_slot_start     TEXT,               -- ISO datetime
    preferred_slot_end       TEXT,
    secondary_slot_start     TEXT,
    secondary_slot_end       TEXT,
    max_date                 TEXT NOT NULL,       -- ISO date
    required_duration_min    INTEGER NOT NULL,
    use_calendar             INTEGER NOT NULL DEFAULT 1,
    special_instructions     TEXT DEFAULT '',
    status                   TEXT DEFAULT 'new', -- mirrors NegotiationState
    created_at               TEXT NOT NULL
);
```

#### [NEW] `app/app.py`
- Flask app factory: `create_app()`
- Registers blueprints (to be added per phase)
- Calls `db.init_db()` on startup
- Mounts voxlayer's `server.create_app()` routes under `/voxlayer` prefix (so one process serves both)

#### [NEW] `app/run.py`
- Starts the bridge (background thread) + the Flask app
- Supplies `booking_loader` to the bridge

#### [NEW] `app/booking_loader.py`
```python
def booking_loader(booking_id: str) -> BookingRequest:
    """The callable voxlayer's bridge.py expects."""
    row = db.get_booking(booking_id)
    # reconstruct fitting_slots from stored JSON
    # reconstruct BookingRequest
    return BookingRequest(...)
```
This is the **main seam** between the host app and voxlayer.

---

### Phase 2 — Google Calendar Integration

**Goal:** Given a required duration + max date, return a list of `SlotWindow` objects from the user's calendar.

#### [NEW] `app/routes/calendar_auth.py`
- `GET /calendar/auth` → redirects to Google OAuth consent screen
- `GET /calendar/callback` → exchanges code, stores credentials in session/DB

#### [NEW] `app/calendar_reader.py`
```python
def get_fitting_slots(
    credentials,
    required_duration_minutes: int,
    max_date: date,
    look_ahead_days: int = 14,
) -> list[SlotWindow]:
    """
    Reads freebusy from Google Calendar for the next `look_ahead_days`.
    Returns windows where a block of `required_duration_minutes` fits,
    capped at max_date.
    """
```
- Uses `google-api-python-client` (`calendar.freebusy().query()`)
- Returns precomputed `SlotWindow` list — passed directly into `BookingRequest.fitting_slots`
- No live write-back in v1

> [!NOTE]
> If the user unchecks "Look for suitable slot in my calendar", the preferred/secondary slots from the form become the `fitting_slots` directly (manual override path).

---

### Phase 3 — Record Management Routes + UI

**Goal:** The "Add new record" and "Use existing record" entry paths.

#### [NEW] `app/routes/records.py`
- `GET  /records` → list all saved business records (JSON, for dropdown)
- `POST /records` → create new record (`{title, target_type, phone}`)
- `GET  /records/<id>` → single record detail

#### [NEW] `app/templates/new_booking.html` (Phase 3 skeleton)
Two UI modes toggled by a tab/radio:

**Mode A — New record:**
```
Title (e.g. "Dr. Smith – Apollo Clinic")  [text]
Target type                                [dropdown: doctor / salon / mechanic / custom]
Phone number                               [text, E.164]
```

**Mode B — Existing record:**
```
Saved service                             [searchable dropdown keyed by phone + title]
```

**Common fields (both modes):**
```
Preferred time slot           [datetime picker]
Secondary slot (optional)     [datetime picker]
Max acceptable date           [date picker]
Required duration             [number, minutes]
[ ] Look for suitable slot in my calendar
Special instructions          [textarea]
```

→ `POST /bookings` on submit

---

### Phase 4 — Booking Creation + Call Placement

**Goal:** Submit the form → compute fitting_slots → store booking → place Twilio call.

#### [NEW] `app/routes/bookings.py`

**`POST /bookings`:**
1. Validate form fields
2. If "Use existing record" → look up `business_records` row
3. If "New record" → insert into `business_records` first
4. If calendar checkbox → call `calendar_reader.get_fitting_slots()` → `fitting_slots`
5. Else → build `SlotWindow` list from preferred + secondary slots
6. Generate `booking_id = str(uuid4())`
7. Store fitting_slots as JSON blob in `bookings` row
8. Build `BookingRequest` + call `caller.place_call(booking)` → get `call_sid`
9. Redirect to `GET /bookings/<booking_id>`

**`GET /bookings/<booking_id>`:**
- Reads `result_store.get_booking_history(booking_id)`
- Renders call log, outcome badge, transcript, summary

---

### Phase 5 — Dashboard (History Table + Status Badges)

**Goal:** The main page — a table of all bookings with live-ish status.

#### [NEW] `app/routes/dashboard.py`
- `GET /` → reads `result_store.get_history_index()` + joins with `bookings` DB rows
- Renders `dashboard.html`

#### [NEW] `app/templates/dashboard.html`
- Table columns: Business name | Type | Status badge | Last call | Summary snippet | Actions
- Status badge colors map to `NegotiationOutcome` taxonomy:
  - `booked` → green
  - `pending_user_approval` → amber (action required)
  - `no_availability` / `declined` → red
  - `voicemail` / `wrong_number` → grey
  - `escalate_to_human` → orange
  - `in_call` → pulsing blue (live)
- "New Booking" button → `/bookings/new`
- Alert banner at top if any pending approvals exist (links to alerts section)

---

### Phase 6 — Alerts Section (Hold Approval Flow)

**Goal:** The dashboard section for holds awaiting user approval. No push — web polling is fine.

#### [MODIFY] `app/routes/alerts.py`
- `GET  /alerts` → `negotiation_state.list_pending_approvals()` → render list
- `POST /alerts/<booking_id>/approve` →
  1. `negotiation_state.approve_held_offer(booking_id)`
  2. Look up booking from DB → rebuild `BookingRequest`
  3. `caller.place_followup_call(booking)` → callback fired
  4. Redirect back to `/alerts`
- `POST /alerts/<booking_id>/reject` → `mark_resolved(booking_id, NegotiationOutcome.DECLINED)` + update DB status

#### Alert card UI (in `dashboard.html` + `/alerts`)
```
┌─────────────────────────────────────────────────────┐
│ ⚠️  Approval Required                                │
│ Dr. Smith – Apollo Clinic offered:                   │
│   Thu Sep 18, 10:00 AM – 11:00 AM                   │
│ They are holding this slot for you.                  │
│                                                      │
│  [✓ Approve — Call Back Now]   [✗ Decline]          │
└─────────────────────────────────────────────────────┘
```

---

### Phase 7 — Tunnel Setup + End-to-End Wiring

**Goal:** Everything works with a real Twilio call.

Two tunnels (same shape as Carecaller):

```
Terminal 1: python run.py
            → Flask app on :5000 (webhook server)
            → Bridge on :8765 (WebSocket)

Terminal 2: ngrok http 5000
            → set VOXLAYER_WEBHOOK_BASE_URL
            → set VOXLAYER_OUTBOUND_TWIML_URL
            → set VOXLAYER_RECORDING_STATUS_CALLBACK_URL

Terminal 3: ngrok http 8765
            → set VOXLAYER_MEDIA_STREAM_URL  (wss://...)
```

`.env` for host app:
```env
SECRET_KEY=...
DB_PATH=app_data/bookapt.db
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://<ngrok-tunnel>/calendar/callback
```

---

### Phase 8 — Polish + Booking Detail View

**Goal:** A clean per-booking page showing full history for multi-call bookings.

#### [NEW] `app/templates/booking_detail.html`
- Business card (name, type, phone)
- Timeline of calls (call 1 → held → user approved → call 2 → booked)
- Per-call: outcome badge, duration, normalized transcript (collapsible), LLM summary
- "Place another call" button (if terminal state)

---

## Dependencies to Add (host app)

```
# app/requirements.txt
flask>=3.0.0,<4.0.0
python-dotenv>=1.0.0,<2.0.0
google-api-python-client>=2.0.0
google-auth-httplib2>=0.2.0
google-auth-oauthlib>=1.0.0
```

---

## Open Questions

> [!IMPORTANT]
> **1. Single-user or multi-user?**
> Currently designed as a single-user local tool (no auth, one Google Calendar). If you need multiple users with separate calendars/histories, the DB schema and calendar credential storage need a `user_id` column and session-scoped OAuth tokens. Which is it?

> [!IMPORTANT]
> **2. One Flask process or two?**
> The bridge (WebSocket server on port 8765) is asyncio-based; Flask is synchronous. The cleanest v1 approach is **two separate processes** (`run_bridge.py` and `run_server.py`) started separately. Alternatively, the bridge can run in a background thread with `asyncio.run_coroutine_threadsafe`. Which do you prefer?

> [!NOTE]
> **3. Google Calendar "manual override"?**
> When the user unchecks the calendar checkbox, the preferred + secondary slots become `fitting_slots`. Should there be a minimum — e.g. if neither slot is set, show a validation error — or is it okay to pass an empty fitting_slots (agent will try anyway, per the bridge prompt)?

> [!NOTE]
> **4. Where does the host app live?**
> Should the host app be a new `app/` folder alongside `bookapt/` (the layout shown above), or should it be a separate repo/folder altogether?

---

## Recommended Next Step

**Start with Phase 1** — scaffold `app/`, get Flask running, create the DB, implement `booking_loader`. This unlocks the ability to test a real call end-to-end before touching the UI.

Phases 1–4 give you a fully working system (no pretty UI, but calls work).
Phases 5–6 give you a usable dashboard.
Phase 7 is the final wiring — needs real Twilio + ngrok.
Phase 8 is polish.
