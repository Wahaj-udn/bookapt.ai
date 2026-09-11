#!/usr/bin/env python3
"""Entrypoint for the voxlayer bridge (WebSocket server).

This starts the Gemini negotiation bridge on port 8765. Run the Flask app
separately with:
    python app/run.py

Architecture
------------
Two-process design:
  - run.py: Flask app (webhooks + UI) on port 5000
  - This process: WebSocket server for live Gemini negotiation on port 8765

Both processes read from the same .env file and share the same database.
The bridge needs `booking_loader` to fetch booking details from the DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to Python path so we can import bookapt and app
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# Load environment variables — app/.env first (for APP_DB_PATH etc.),
# then bookapt/.env (for Twilio / Gemini credentials).
from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env")
load_dotenv(_ROOT / "bookapt" / ".env", override=True)

# Now we can import the bridge and booking_loader
from bookapt import bridge
from app.booking_loader import booking_loader


def main() -> None:
    """Start the voxlayer bridge."""
    print()
    print("=" * 60)
    print("    BookAppt.ai - Voxlayer Bridge (WebSocket)")
    print("=" * 60)
    print()
    print("  > WebSocket server starting on port 8765")
    print("  > Waiting for incoming Twilio media streams...")
    print()
    print("  [i] Make sure the Flask app is running:")
    print("      python app/run.py")
    print()

    # Start the bridge with our booking_loader
    # The bridge will call booking_loader(booking_id) whenever a call comes in
    bridge.main(booking_loader=booking_loader)


if __name__ == "__main__":
    main()
