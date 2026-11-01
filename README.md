# BookAppt.ai

> An AI-powered appointment booking agent that calls businesses on your behalf, negotiates a time slot, and writes the confirmed appointment back to your Google Calendar.

---

## How It Works

You fill out a booking form (business name, phone number, appointment type, preferred time). The system:

1. Reads your Google Calendar to find free windows in the requested date range.
2. Places an outbound phone call to the business via Twilio.
3. Connects the live call audio to **Gemini Live**, which handles the conversation naturally — greeting the receptionist, proposing times, and checking every slot against your actual calendar before accepting.
4. After the call, transcribes the recording, classifies the outcome (booked / pending approval / no availability / etc.), and sends you a WhatsApp notification.
5. If the appointment was confirmed, creates a Google Calendar event automatically.

If the business proposes a "hold" (tentative slot pending your confirmation), you receive a WhatsApp message and can reply **"callback"** to trigger a follow-up call that locks in the time.

---

## Architecture

The system runs as **two separate processes** that share a SQLite database and `.env` config:

```
┌──────────────────────────────────────┐     ┌──────────────────────────────────┐
│  Flask Host App  (port 5000)         │     │  Voxlayer Bridge  (port 8765)    │
│                                      │     │                                  │
│  • Dashboard UI                      │     │  • WebSocket server              │
│  • Booking creation form             │     │  • Twilio ↔ Gemini Live bridge   │
│  • Google Calendar OAuth             │     │  • Slot-checking tool            │
│  • WhatsApp webhook (/whatsapp)      │     │  • Conversation logging          │
│  • Voxlayer webhooks (/voxlayer)     │     │                                  │
│    - POST /voice/outbound (TwiML)    │     │  Reads BookingRequest from DB    │
│    - POST /voice/recording           │     │  via booking_loader              │
│  • Admin / retry queue               │     └──────────────────────────────────┘
└──────────────────────────────────────┘
                   │
         Shared SQLite DB + .env
```

### Post-Call Pipeline

After every call recording is received:

```
Recording (MP3) → Whisper transcription → Transcript alignment
→ Normalization → LLM extraction (outcome, confirmed times, summary)
→ Result store → WhatsApp notification → Google Calendar write-back
```

Failed pipeline runs are saved to a retry queue and can be re-triggered from the admin panel.

---

## Project Structure

```
MRCET/
├── app/                        # Flask host application
│   ├── app.py                  # Application factory (Flask)
│   ├── run.py                  # Entry point — starts Flask on port 5000
│   ├── run_bridge.py           # Entry point — starts WebSocket bridge on port 8765
│   ├── db.py                   # SQLite layer (business_records + bookings tables)
│   ├── booking_loader.py       # Reads a booking from DB → BookingRequest
│   ├── calendar_reader.py      # Google Calendar OAuth, free/busy, event write-back
│   ├── config.py               # Flask config (reads from .env)
│   ├── routes/
│   │   ├── dashboard.py        # Main dashboard page
│   │   ├── providers.py        # Business record CRUD
│   │   ├── bookings.py         # Booking creation + call placement
│   │   ├── calendar_auth.py    # Google OAuth flow (/calendar/auth, /calendar/callback)
│   │   ├── whatsapp.py         # WhatsApp incoming webhook (/whatsapp/incoming)
│   │   └── admin.py            # Pipeline retry queue (/admin/retry-queue)
│   ├── templates/              # Jinja2 HTML templates
│   ├── static/                 # CSS / JS assets
│   └── requirements.txt
│
├── bookapt/                    # Voxlayer calling engine (standalone package)
│   ├── models.py               # Shared data contract: BookingRequest, CallResult
│   ├── bridge.py               # Twilio ↔ Gemini Live WebSocket bridge
│   ├── server.py               # Twilio voice webhook server (Flask)
│   ├── caller.py               # Places outbound calls via Twilio REST API
│   ├── slot_matcher.py         # Deterministic slot-fit checker (tool exposed to Gemini)
│   ├── busy_store.py           # JSON cache of busy blocks per booking
│   ├── negotiation_state.py    # Tracks hold/pending-approval state across calls
│   ├── extractor.py            # LLM-based outcome extraction from transcripts
│   ├── normalizer.py           # Transcript normalization
│   ├── transcriber.py          # Whisper (faster-whisper) transcription
│   ├── transcript_builder.py   # Aligns Whisper output with conversation logs
│   ├── result_store.py         # Persists CallResult to disk
│   ├── whatsapp_notifier.py    # Sends post-call WhatsApp messages via Twilio
│   ├── pipeline_retry_queue.py # Queues failed post-call pipeline jobs
│   ├── gemini_retry.py         # Retry wrapper for Gemini API calls
│   ├── mission_prompt.md       # Agent system prompt template
│   └── requirements.txt
│
└── bookapt_data/               # Runtime data (auto-created, gitignored)
    ├── bookapt.db              # SQLite database
    ├── recordings/             # Downloaded call MP3s
    ├── conversations/          # Live conversation logs (per call)
    ├── results/                # Extracted CallResult JSON files
    ├── final_transcripts/      # Aligned transcripts
    └── whisper_transcripts/    # Raw Whisper output
```

---

## Prerequisites

- **Python 3.11 or 3.13+** (3.12 works; `audioop` was removed in 3.13 so `audioop-lts` is installed automatically)
- A **Twilio** account with:
  - A phone number capable of outbound calls
  - A WhatsApp-enabled number / Sandbox (for notifications)
- A **Google Cloud project** with the Calendar API enabled and OAuth 2.0 credentials
- A **Gemini API key** with access to the Gemini Live (streaming) API
- **ngrok** (or any tunnel) to expose local ports to Twilio webhooks

---

## Setup

### 1. Clone and create virtual environments

```bash
# Host app
cd app
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt

# Voxlayer engine (separate venv recommended)
cd ../bookapt
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy the example file and fill in your credentials:

```bash
cp app/.env.example app/.env
```

| Variable | Description |
|---|---|
| `APP_SECRET_KEY` | Flask session secret key |
| `APP_DB_PATH` | Path to the SQLite database file |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `GOOGLE_REDIRECT_URI` | OAuth redirect URI (e.g. `http://localhost:5000/calendar/callback`) |
| `GOOGLE_TOKEN_PATH` | Where to persist the OAuth token JSON |
| `GOOGLE_CALENDAR_TIMEZONE` | IANA timezone (e.g. `Asia/Kolkata`) |
| `VOXLAYER_TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `VOXLAYER_TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `VOXLAYER_TWILIO_PHONE_NUMBER` | Your Twilio number in E.164 format |
| `VOXLAYER_GEMINI_API_KEY` | Gemini API key |
| `VOXLAYER_WEBHOOK_BASE_URL` | Public base URL (ngrok) for Twilio webhooks |
| `VOXLAYER_OUTBOUND_TWIML_URL` | Full URL: `{base}/voxlayer/voice/outbound` |
| `VOXLAYER_RECORDING_STATUS_CALLBACK_URL` | Full URL: `{base}/voxlayer/voice/recording` |
| `VOXLAYER_MEDIA_STREAM_URL` | WebSocket URL for the bridge: `wss://{bridge-tunnel}/media-stream` |

> **Note:** The bridge needs its own public ngrok tunnel (separate from the Flask app) because Twilio opens a WebSocket connection to the bridge directly.

### 3. Set up Google Calendar OAuth

1. Go to the [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials.
2. Create an **OAuth 2.0 Client ID** for a Web Application.
3. Add `http://localhost:5000/calendar/callback` as an authorized redirect URI.
4. Download the credentials and copy `client_id` / `client_secret` into your `.env`.

### 4. Configure Twilio webhooks

In the Twilio Console, set these URLs (using your ngrok tunnel):

| Webhook | URL |
|---|---|
| Voice: "A call comes in" | `POST https://your-ngrok.io/voxlayer/voice/outbound` |
| Recording status callback | `POST https://your-ngrok.io/voxlayer/voice/recording` |
| WhatsApp "When a message comes in" | `POST https://your-ngrok.io/whatsapp/incoming` |

---

## Running

Start both processes in separate terminals:

```bash
# Terminal 1 — Flask host app (port 5000)
python app/run.py

# Terminal 2 — Voxlayer bridge (port 8765)
python app/run_bridge.py
```

Then open `http://localhost:5000` in your browser.

---

## Usage

1. **Connect Google Calendar** — visit `/calendar/auth` and complete the OAuth flow. This is required for free/busy detection and calendar write-back.

2. **Add a provider** — go to the Providers section and save a business (name, type, phone number). Saved providers auto-fill the booking form on future use.

3. **Create a booking** — fill in the appointment details (duration, deadline, preferred time, any special instructions). On submit, the system queries your calendar, computes free slots, and immediately places the outbound call.

4. **Wait for the call to finish** — watch the dashboard for live status updates. When the call ends, the post-call pipeline runs automatically.

5. **Review results** — the dashboard shows the outcome, confirmed time, a call summary, and a link to the full transcript. If the outcome is `booked`, the event appears in your Google Calendar within seconds.

6. **"Hold" flow** — if the business could only tentatively hold a slot, you'll receive a WhatsApp message. Reply **"callback"** to trigger the AI to call back and confirm.

---

## Key Design Decisions

**Slot checking is deterministic, not LLM-based.** The agent has access to a `check_slot_fits` tool backed by `slot_matcher.py`. Before saying any slot is confirmed, the model *must* call this tool. The tool does exact overlap detection against your calendar's busy blocks — the model never guesses or hallucinates availability.

**Outcome classification happens post-call.** The bridge only conducts the conversation and writes a log. Whether the call resulted in a booking, a hold, no availability, etc., is determined after the fact by `extractor.py` reading the full transcript. This keeps the bridge simple and outcome logic testable without live calls.

**Two separate processes share one DB.** The Flask app and the bridge are designed to run independently. The bridge fetches booking details through a `booking_loader` function, so the engine (`bookapt/`) has no direct dependency on Flask or the DB schema — it only knows about `BookingRequest` and `CallResult`.

**Failed pipeline runs are retried, not lost.** If transcription or extraction fails for any reason, the job is saved to a JSON retry queue. An admin endpoint (`/admin/retry-queue`) lists pending failures and lets you re-trigger them.

---

## API Endpoints

### Flask Host App (`port 5000`)

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard |
| GET/POST | `/calendar/auth` | Start Google OAuth flow |
| GET | `/calendar/callback` | OAuth callback |
| GET | `/calendar/status` | Calendar connection status |
| POST | `/bookings/create` | Create a booking and place a call |
| GET | `/bookings/<id>` | Booking detail page |
| POST | `/whatsapp/incoming` | WhatsApp message webhook |
| GET | `/admin/retry-queue` | View failed pipeline jobs |
| POST | `/admin/retry-pipeline` | Re-run a failed pipeline job |
| GET | `/health` | Health check |

### Voxlayer Webhooks (mounted under `/voxlayer`)

| Method | Path | Description |
|---|---|---|
| POST | `/voxlayer/voice/outbound` | TwiML response to connect call to bridge |
| POST | `/voxlayer/voice/recording` | Recording-complete callback; triggers pipeline |
| GET | `/voxlayer/health` | Health check |

---

## Dependencies

**Host app** (`app/requirements.txt`):
- `flask` — web framework
- `python-dotenv` — `.env` loading
- `google-api-python-client`, `google-auth-oauthlib` — Google Calendar integration
- `twilio` — WhatsApp notifications, call placement
- `google-genai` — Gemini API client
- `websockets` — bridge WebSocket server
- `faster-whisper` — local speech transcription

**Voxlayer engine** (`bookapt/requirements.txt`): same core set; `audioop-lts` is added as a drop-in for Python 3.13+ where `audioop` was removed from stdlib.

---

## License

MIT
