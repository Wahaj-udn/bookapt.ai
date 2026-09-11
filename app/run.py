#!/usr/bin/env python3
"""Entrypoint for the BookAppt.ai Flask host application.

This starts the web server on port 5000. Run the bridge separately with:
    python app/run_bridge.py

Architecture
------------
Two-process design:
  - This process: Flask app (webhooks + UI)
  - run_bridge.py: WebSocket server for live Gemini negotiation

Both processes read from the same .env file and share the same database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add project root to Python path so we can import bookapt and app
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env")

# Now we can import the app
from app.app import create_app


def main() -> None:
    """Start the Flask application."""
    app = create_app()

    # Read host/port from environment (for deployment flexibility)
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "5000"))
    debug = os.environ.get("APP_DEBUG", "1") == "1"

    print()
    print("=" * 60)
    print("     BookAppt.ai - Host Application (Flask)")
    print("=" * 60)
    print()
    print(f"  > Running on http://{host}:{port}")
    print(f"  > Debug mode: {debug}")
    print()
    print("  [!] Don't forget to start the bridge in a separate terminal:")
    print("      python app/run_bridge.py")
    print()

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
