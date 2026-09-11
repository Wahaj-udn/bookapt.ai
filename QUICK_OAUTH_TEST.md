# Quick Setup Guide — Test OAuth Right Now

## What This Does

This is a **standalone test script** to verify Google Calendar OAuth works **without** requiring the full BookAppt.ai setup. It runs on port 5001 (different from your main app).

---

## Step 1: Get Google OAuth Credentials (5 minutes)

1. **Go to:** https://console.cloud.google.com/apis/credentials

2. **Create/select a project**

3. **Enable Google Calendar API:**
   - Click "Enable APIs and Services"
   - Search for "Google Calendar API"
   - Click "Enable"

4. **Create OAuth 2.0 Client ID:**
   - Click "Create Credentials" → "OAuth 2.0 Client ID"
   - If prompted, configure OAuth consent screen first:
     - User type: **External**
     - App name: **BookAppt Test**
     - User support email: your email
     - Developer contact: your email
     - Save and continue (skip scopes, skip test users)
   - Then create OAuth client:
     - Application type: **Web application**
     - Name: **BookAppt Test**
     - Authorized redirect URIs: **`http://127.0.0.1:5001/callback`** ← IMPORTANT!
     - Click "Create"

5. **Copy credentials:**
   - You'll see a popup with Client ID and Client Secret
   - Copy both

---

## Step 2: Add Credentials to .env (30 seconds)

Open `app/.env` and add these lines (or create it if it doesn't exist):

```env
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your-secret-here
```

**Example:**
```env
GOOGLE_CLIENT_ID=123456789-abc123xyz.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-Ab12cD34eF56gH78iJ90
```

---

## Step 3: Run the Test Script (10 seconds)

```bash
cd D:\Wahaj\Projects\Hackathons\MRCET
./app/venv/Scripts/python.exe demo_oauth_test.py
```

You should see:
```
============================================================
     Quick Google Calendar OAuth Test
============================================================

  → Open in browser: http://127.0.0.1:5001

  Make sure you have in app/.env:
    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...

  Redirect URI to add in Google Console:
    http://127.0.0.1:5001/callback

============================================================
```

---

## Step 4: Test in Browser (1 minute)

1. **Open:** http://127.0.0.1:5001

2. **Click** "Connect Google Calendar"

3. **Select** your Google account

4. **Review permissions:**
   - See your email address
   - Read your calendar events
   
5. **Click** "Allow"

6. **You'll be redirected back** and see:
   - ✅ Connected status
   - Your email
   - List of upcoming events
   - Busy blocks for next 7 days

---

## What You'll See

### Before OAuth:
```
📅 Quick Google Calendar OAuth Test

This is a temporary test to verify OAuth works.

[Connect Google Calendar] ← Click this
```

### After OAuth:
```
✅ Connected to Google Calendar

Account: your-email@gmail.com

📅 Upcoming Events (Next 10)
• Team Meeting — 2026-09-12T10:00:00
• Lunch with Sarah — 2026-09-13T12:30:00
...

🕒 Busy Blocks (Next 7 Days)
• 2026-09-12T10:00:00 → 2026-09-12T11:00:00
• 2026-09-13T12:30:00 → 2026-09-13T13:30:00
...

🎯 What This Proves
✅ OAuth flow works
✅ Can read your calendar
✅ Can query free/busy (used for slot detection)
✅ Ready to integrate with BookAppt.ai
```

---

## Troubleshooting

### Error: "redirect_uri_mismatch"
**Fix:** Make sure you added **exactly** `http://127.0.0.1:5001/callback` to Google Console (not `localhost`, not `5000`).

### Error: "Configuration Missing"
**Fix:** Check that `app/.env` has the correct Client ID and Secret (no quotes, no extra spaces).

### Error: "invalid_grant"
**Fix:** Try clicking "Disconnect" and reconnecting. Sometimes tokens get stale during development.

### Can't see events
**Fix:** Make sure you have events in your Google Calendar. Add a test event if needed.

---

## What This Proves

When this works, you've verified:
- ✅ Google OAuth setup is correct
- ✅ Can read calendar data
- ✅ Free/busy API works (this is what BookAppt.ai uses)
- ✅ Ready for full Phase 2 integration

---

## Next Steps

Once OAuth works in the test script:

1. **Stop the test script** (Ctrl+C)

2. **Use the same credentials in your main app:**
   - They're already in `app/.env`
   - Just run `python app/run.py` instead
   - Visit http://localhost:5000/calendar/auth

3. **The main app** has the full Phase 2 integration with `get_fitting_slots()`

---

## Quick Command Reference

**Run test:**
```bash
./app/venv/Scripts/python.exe demo_oauth_test.py
```

**Open test:**
```
http://127.0.0.1:5001
```

**Stop test:**
Press `Ctrl+C`

**Run main app (after test works):**
```bash
./app/venv/Scripts/python.exe app/run.py
```

**Main app URL:**
```
http://localhost:5000
```
