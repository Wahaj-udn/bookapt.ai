#!/usr/bin/env python3
"""Provider routes — CRUD for saved business records.

Handles:
  - List all saved providers
  - Add new provider
  - Edit provider details
  - Delete provider
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

try:
    from .. import db
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from app import db

providers_bp = Blueprint("providers", __name__, url_prefix="/providers")


@providers_bp.route("/")
def list_providers():
    """Show all saved providers with add/edit/delete options."""
    providers = db.get_all_business_records()
    return render_template("providers.html", providers=providers)


@providers_bp.route("/add", methods=["POST"])
def add_provider():
    """Create a new business record from form data."""

    # Get form data
    provider_type = request.form.get("provider_type", "").strip()
    business_name = request.form.get("business_name", "").strip()
    phone = request.form.get("phone", "").strip()
    address = request.form.get("address", "").strip()
    notes = request.form.get("notes", "").strip()

    # Validation
    if not business_name:
        flash("Business name is required", "error")
        return redirect(url_for("providers.list_providers"))

    if not phone:
        flash("Phone number is required", "error")
        return redirect(url_for("providers.list_providers"))

    if not provider_type:
        provider_type = "Other"

    # Create full title with address if provided
    title = business_name
    if address:
        title = f"{business_name} — {address}"

    # Add notes to title if provided
    if notes:
        title = f"{title} ({notes})"

    # Create business record
    try:
        record = db.create_business_record(
            title=title,
            target_type=provider_type.lower(),
            phone=phone,
        )
        flash(f"✓ Provider '{business_name}' saved successfully!", "success")
    except Exception as e:
        flash(f"Error saving provider: {e}", "error")

    return redirect(url_for("providers.list_providers"))


@providers_bp.route("/delete/<int:provider_id>", methods=["POST"])
def delete_provider(provider_id):
    """Delete a business record."""
    # TODO: Implement delete functionality in db.py
    flash("Provider deleted", "info")
    return redirect(url_for("providers.list_providers"))


@providers_bp.route("/<int:provider_id>")
def view_provider(provider_id):
    """View single provider details (JSON)."""
    provider = db.get_business_record(provider_id)
    if provider:
        return jsonify(provider)
    return jsonify({"error": "Provider not found"}), 404
