#!/usr/bin/env python3
"""Host-application configuration.

All values are read lazily from environment variables (populated by a .env
file loaded before this module is imported). The .env.example in this folder
documents every variable.
"""

from __future__ import annotations

import os
from pathlib import Path

# Resolve the root of the monorepo (one level above app/)
_ROOT = Path(__file__).parent.parent


class Config:
    # ── Flask ────────────────────────────────────────────────────────────────
    SECRET_KEY: str = os.environ.get("APP_SECRET_KEY", "dev-secret-change-in-prod")

    # ── SQLite database ──────────────────────────────────────────────────────
    DB_PATH: str = os.environ.get(
        "APP_DB_PATH", str(_ROOT / "bookapt_data" / "bookapt.db")
    )

    # ── Google Calendar OAuth2 ───────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI: str = os.environ.get(
        "GOOGLE_REDIRECT_URI", "http://localhost:5000/calendar/callback"
    )
    GOOGLE_TOKEN_PATH: str = os.environ.get(
        "GOOGLE_TOKEN_PATH", str(_ROOT / "bookapt_data" / "google_token.json")
    )
    GOOGLE_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/calendar.readonly"
    ]
