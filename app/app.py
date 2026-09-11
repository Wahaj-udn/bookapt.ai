#!/usr/bin/env python3
"""Flask application factory for BookAppt.ai host application.

This is the main Flask app that serves:
  - Dashboard UI (Phase 5)
  - Booking creation form (Phase 3-4)
  - Calendar OAuth flow (Phase 2)
  - Alerts/approval UI (Phase 6)
  - Voxlayer webhook endpoints (mounted under /voxlayer)

The app runs in one process (run.py); the bridge runs separately (run_bridge.py).
"""

from __future__ import annotations

from flask import Flask

try:
    from . import config, db
except ImportError:
    # Allow running as script for testing
    import config
    import db


def create_app() -> Flask:
    """Application factory — creates and configures the Flask app."""
    app = Flask(__name__)

    # ── Configuration ────────────────────────────────────────────────────────
    app.config.from_object(config.Config)

    # ── Database initialization ──────────────────────────────────────────────
    # Create tables if they don't exist (safe to call on every startup)
    with app.app_context():
        db.init_db()

    # ── Mount voxlayer webhook server ────────────────────────────────────────
    # The voxlayer engine has its own Flask app (bookapt.server.create_app).
    # We mount it using DispatcherMiddleware so all webhooks go to one process.
    try:
        from werkzeug.middleware.dispatcher import DispatcherMiddleware
        from bookapt.server import create_app as create_voxlayer_app
        voxlayer_app = create_voxlayer_app()
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
            '/voxlayer': voxlayer_app
        })
        app.logger.info("✓ Mounted voxlayer webhook routes under /voxlayer")
    except ImportError as e:
        app.logger.warning(f"⚠ Could not mount voxlayer routes: {e}")
        app.logger.warning("  (This is fine if testing without the bridge)")

    # ── Register blueprints ──────────────────────────────────────────────────

    # Dashboard routes (main pages)
    try:
        from app.routes.dashboard import dashboard_bp
        app.register_blueprint(dashboard_bp)
        app.logger.info("✓ Registered dashboard routes")
    except ImportError as e:
        app.logger.warning(f"⚠ Could not register dashboard routes: {e}")

    # Provider management routes
    try:
        from app.routes.providers import providers_bp
        app.register_blueprint(providers_bp)
        app.logger.info("✓ Registered provider routes")
    except ImportError as e:
        app.logger.warning(f"⚠ Could not register provider routes: {e}")

    # Booking creation and call placement routes
    try:
        from app.routes.bookings import bookings_bp
        app.register_blueprint(bookings_bp)
        app.logger.info("✓ Registered booking routes")
    except ImportError as e:
        app.logger.warning(f"⚠ Could not register booking routes: {e}")

    # Google Calendar OAuth (Phase 2)
    try:
        from app.routes.calendar_auth import calendar_bp
        app.register_blueprint(calendar_bp)
        app.logger.info("✓ Registered calendar OAuth routes")
    except ImportError as e:
        app.logger.warning(f"⚠ Could not register calendar routes: {e}")

    # TODO: Register additional blueprints:
    #   - routes.alerts (Phase 6) - Hold approval flow

    # ── Health check endpoint ────────────────────────────────────────────────
    @app.route("/health")
    def health_check():
        """Simple health check — useful for monitoring and testing."""
        return {"status": "ok", "service": "bookapt-host-app"}, 200

    # Note: Dashboard blueprint handles / route now
    # This is just a fallback if blueprints fail to load

    return app
