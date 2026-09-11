#!/usr/bin/env python3
"""Dashboard routes — main landing page and booking initiation.

This is the entry point after login. Shows:
  - Quick booking form (select provider type)
  - Saved providers list
  - Recent activity
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from datetime import datetime

try:
    from .. import db
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from app import db

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    """Main dashboard — show saved providers and quick booking form."""

    # Get user info from session (if logged in)
    user_name = session.get("user_name", "Guest")

    # Get all saved business records
    saved_providers = db.get_all_business_records()

    # Get recent bookings
    recent_bookings = db.get_all_bookings()[:5]  # Last 5 bookings

    return render_template(
        "dashboard.html",
        user_name=user_name,
        saved_providers=saved_providers,
        recent_bookings=recent_bookings,
    )


@dashboard_bp.route("/home")
@dashboard_bp.route("/index")
def home():
    """Landing page (marketing page)."""
    return render_template("index.html")
