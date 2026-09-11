#!/usr/bin/env python3
"""Diagnostic tool - shows exactly what redirect URI will be sent to Google."""

import os
from dotenv import load_dotenv

load_dotenv("app/.env")

print()
print("=" * 70)
print("  GOOGLE OAUTH REDIRECT URI DIAGNOSTIC")
print("=" * 70)
print()

client_id = os.getenv("GOOGLE_CLIENT_ID", "")
print(f"Your Client ID (from .env):")
print(f"  {client_id}")
print()

redirect_uri = "http://127.0.0.1:5001/callback"
print(f"Redirect URI the app will send to Google:")
print(f"  {redirect_uri}")
print()

print("=" * 70)
print("  INSTRUCTIONS TO FIX")
print("=" * 70)
print()
print("1. Go to: https://console.cloud.google.com/apis/credentials")
print()
print(f"2. Find and click on OAuth Client: {client_id[:20]}...")
print()
print("3. In 'Authorized redirect URIs' section, add EXACTLY this:")
print(f"   {redirect_uri}")
print()
print("4. Common mistakes to avoid:")
print("   ✗ http://localhost:5001/callback  (wrong - says 'localhost')")
print("   ✗ https://127.0.0.1:5001/callback (wrong - has 'https')")
print("   ✗ http://127.0.0.1:5000/callback  (wrong - port 5000)")
print("   ✓ http://127.0.0.1:5001/callback  (correct!)")
print()
print("5. Click SAVE at the bottom")
print()
print("6. Wait 2-5 minutes for Google to propagate the change")
print()
print("=" * 70)
print()
print("After saving, wait 3-5 minutes, then try again:")
print("  http://127.0.0.1:5001")
print()
