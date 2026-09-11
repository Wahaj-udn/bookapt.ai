#!/usr/bin/env python3
"""Quick OAuth test — tries LOCALHOST instead of 127.0.0.1.

This version uses http://localhost:5001/callback as the redirect URI,
which is what your friend likely configured in Google Console.
"""

import os
from datetime import datetime, timedelta
from flask import Flask, redirect, request, session, url_for
from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

load_dotenv("app/.env")

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
app.secret_key = os.urandom(24)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar.readonly",
]

CLIENT_CONFIG = {
    "web": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

# CHANGED: localhost instead of 127.0.0.1
REDIRECT_URI = "http://localhost:5001/callback"


def creds_to_dict(creds):
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }


def get_credentials():
    if "credentials" not in session:
        return None

    creds = Credentials(**session["credentials"])

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        session["credentials"] = creds_to_dict(creds)

    return creds


@app.route("/")
def home():
    if "credentials" not in session:
        return """
        <html>
        <head><title>Quick OAuth Test</title></head>
        <body style="font-family: sans-serif; max-width: 600px; margin: 100px auto;">
            <h1>Quick Google Calendar OAuth Test</h1>
            <p>This is a temporary test to verify OAuth works.</p>
            <p><strong>Redirect URI:</strong> <code>http://localhost:5001/callback</code></p>
            <p><a href="/login" style="display: inline-block; padding: 10px 20px; background: #4285f4; color: white; text-decoration: none; border-radius: 4px;">Connect Google Calendar</a></p>
            <hr>
            <p><strong>Setup checklist:</strong></p>
            <ol>
                <li>Google OAuth credentials in app/.env</li>
                <li>Redirect URI in Google Console: <code>http://localhost:5001/callback</code></li>
                <li>Google Calendar API enabled</li>
            </ol>
        </body>
        </html>
        """

    creds = get_credentials()
    user_email = session.get("user_email", "Unknown")

    service = build("calendar", "v3", credentials=creds)
    now = datetime.utcnow().isoformat() + "Z"

    try:
        events_result = service.events().list(
            calendarId="primary",
            timeMin=now,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])
    except Exception as e:
        events = []

    try:
        time_min = datetime.utcnow().isoformat() + "Z"
        time_max = (datetime.utcnow() + timedelta(days=7)).isoformat() + "Z"

        freebusy_result = service.freebusy().query(body={
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": "primary"}],
        }).execute()

        busy_blocks = freebusy_result.get("calendars", {}).get("primary", {}).get("busy", [])
    except Exception as e:
        busy_blocks = []

    events_html = ""
    if events:
        events_html = "<ul>"
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            summary = event.get("summary", "No title")
            events_html += f"<li><strong>{summary}</strong> — {start}</li>"
        events_html += "</ul>"
    else:
        events_html = "<p><em>No upcoming events found.</em></p>"

    busy_html = ""
    if busy_blocks:
        busy_html = "<ul>"
        for block in busy_blocks[:10]:
            start = block["start"]
            end = block["end"]
            busy_html += f"<li>{start} -> {end}</li>"
        busy_html += "</ul>"
    else:
        busy_html = "<p><em>No busy blocks in next 7 days.</em></p>"

    return f"""
    <html>
    <head><title>Connected!</title></head>
    <body style="font-family: sans-serif; max-width: 800px; margin: 50px auto;">
        <h1>Connected to Google Calendar</h1>
        <p><strong>Account:</strong> {user_email}</p>
        <p><a href="/logout">Disconnect</a></p>

        <hr>

        <h2>Upcoming Events (Next 10)</h2>
        {events_html}

        <hr>

        <h2>Busy Blocks (Next 7 Days)</h2>
        <p><em>This is what the free/busy API returns - used to find available slots.</em></p>
        {busy_html}

        <hr>

        <h2>What This Proves</h2>
        <ul>
            <li>OAuth flow works</li>
            <li>Can read your calendar</li>
            <li>Can query free/busy (used for slot detection)</li>
            <li>Ready to integrate with BookAppt.ai</li>
        </ul>

        <p><a href="/logout">Disconnect and try again</a></p>
    </body>
    </html>
    """


@app.route("/login")
def login():
    if not CLIENT_CONFIG["web"]["client_id"]:
        return """
        <html>
        <body style="font-family: sans-serif; max-width: 600px; margin: 100px auto;">
            <h1>Configuration Missing</h1>
            <p>Please add Google OAuth credentials to <code>app/.env</code></p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """

    flow = Flow.from_client_config(CLIENT_CONFIG, scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    session["state"] = state
    session["code_verifier"] = flow.code_verifier
    return redirect(authorization_url)


@app.route("/callback")
def callback():
    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        state=session["state"],
    )
    flow.redirect_uri = REDIRECT_URI
    flow.code_verifier = session.get("code_verifier")

    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        session["credentials"] = creds_to_dict(creds)

        oauth2_service = build("oauth2", "v2", credentials=creds)
        user_info = oauth2_service.userinfo().get().execute()
        session["user_email"] = user_info.get("email")

        return redirect("/")
    except Exception as e:
        return f"""
        <html>
        <body style="font-family: sans-serif; max-width: 600px; margin: 100px auto;">
            <h1>OAuth Error</h1>
            <p><strong>Error:</strong> {e}</p>
            <p><a href="/login">Try again</a></p>
        </body>
        </html>
        """


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    print()
    print("=" * 60)
    print("     Quick Google Calendar OAuth Test")
    print("     Using LOCALHOST redirect URI")
    print("=" * 60)
    print()
    print("  > Open in browser: http://localhost:5001")
    print()
    print("  Redirect URI: http://localhost:5001/callback")
    print()
    print("=" * 60)
    print()

    app.run(debug=True, port=5001, host="127.0.0.1")
