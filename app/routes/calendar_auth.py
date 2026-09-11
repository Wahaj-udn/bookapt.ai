#!/usr/bin/env python3
"""Google Calendar OAuth2 flow routes.

Implements the three-step OAuth dance:
  1. GET /calendar/auth        → redirect to Google consent screen
  2. GET /calendar/callback    → handle authorization code, store credentials
  3. GET /calendar/logout      → clear stored credentials

Credentials are stored persistently in a file (not session), so they survive
server restarts. Token refresh happens automatically in calendar_reader.py.
"""

from __future__ import annotations

import os

from flask import Blueprint, redirect, request, session, url_for, flash
from google_auth_oauthlib.flow import Flow

try:
    from .. import config, calendar_reader
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from app import config, calendar_reader


# Allow HTTP in development (remove in production — use HTTPS + ngrok)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

calendar_bp = Blueprint("calendar", __name__, url_prefix="/calendar")

# OAuth scopes — read-only for free/busy queries
# If you later want to write events back (Phase 8+), add "calendar.events"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",  # Read-only for free/busy
]


def _build_flow(state: str = None) -> Flow:
    """Build a Flow object with the app's OAuth config."""
    client_config = {
        "web": {
            "client_id": config.Config.GOOGLE_CLIENT_ID,
            "client_secret": config.Config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = config.Config.GOOGLE_REDIRECT_URI
    return flow


# --------------------------------------------------------------------------
# OAuth routes
# --------------------------------------------------------------------------

@calendar_bp.route("/auth")
def auth():
    """
    Step 1: Redirect user to Google consent screen.

    This initiates the OAuth flow. Google will ask the user to:
      1. Select a Google account
      2. Review permissions (calendar read access)
      3. Approve or deny

    After approval, Google redirects to /calendar/callback with an auth code.
    """
    flow = _build_flow()

    authorization_url, state = flow.authorization_url(
        access_type="offline",       # Request a refresh token
        include_granted_scopes="true",
        prompt="consent",            # Force consent screen (ensures refresh_token)
    )

    # Store state and PKCE verifier in session for validation in callback
    session["oauth_state"] = state
    session["code_verifier"] = flow.code_verifier

    return redirect(authorization_url)


@calendar_bp.route("/callback")
def callback():
    """
    Step 2: Handle OAuth callback from Google.

    Google redirects here after user approval, with:
      - code: authorization code (exchange for access token)
      - state: CSRF protection token (must match session)

    We exchange the code for tokens and store them persistently.
    """
    # Validate state to prevent CSRF attacks
    state = session.get("oauth_state")
    if not state or state != request.args.get("state"):
        flash("OAuth state mismatch. Please try again.", "error")
        return redirect("/")

    flow = _build_flow(state=state)
    flow.code_verifier = session.get("code_verifier")  # Restore PKCE verifier

    try:
        # Exchange authorization code for access token + refresh token
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        # Save credentials to file (persistent storage)
        calendar_reader.save_credentials(creds)

        # Optional: fetch user info for display
        user_info = calendar_reader.get_user_info()
        if user_info:
            session["user_email"] = user_info.get("email")
            session["user_name"] = user_info.get("name")

        # Clear OAuth session state
        session.pop("oauth_state", None)
        session.pop("code_verifier", None)

        flash(f"Successfully connected Google Calendar for {user_info.get('email', 'your account')}!", "success")
        return redirect("/")

    except Exception as e:
        flash(f"OAuth error: {e}", "error")
        return redirect("/")


@calendar_bp.route("/logout")
def logout():
    """
    Step 3: Clear stored credentials and session data.

    This logs the user out of Google Calendar integration (does NOT revoke
    the token on Google's side — user can do that in their Google account).
    """
    calendar_reader.clear_credentials()
    session.pop("user_email", None)
    session.pop("user_name", None)

    flash("Google Calendar disconnected.", "info")
    return redirect("/")


@calendar_bp.route("/status")
def status():
    """
    Helper route: Check if user has valid credentials.

    Returns JSON with connection status. Useful for dashboard widgets.
    """
    creds = calendar_reader.load_credentials()
    user_info = calendar_reader.get_user_info() if creds else None

    return {
        "connected": creds is not None,
        "user": user_info,
    }
