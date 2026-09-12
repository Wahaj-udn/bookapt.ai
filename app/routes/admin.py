#!/usr/bin/env python3
"""Admin routes — pipeline retry queue management.

Endpoints:
  GET  /admin/retry-queue      — view pending failed pipeline entries (JSON)
  POST /admin/retry-pipeline   — trigger retry for all queued entries (JSON)

These are intentionally simple with no auth (hackathon context). Add auth
middleware before deploying externally.
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, jsonify

LOGGER = logging.getLogger("bookapt.admin")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.get("/retry-queue")
def view_retry_queue() -> Response:
    """Return the current failed pipeline queue as JSON."""
    try:
        from bookapt.pipeline_retry_queue import list_queue
        entries = list_queue()
        return jsonify({
            "count": len(entries),
            "entries": entries,
        })
    except Exception as exc:
        LOGGER.error("Error reading retry queue: %s", exc)
        return jsonify({"error": str(exc)}), 500


@admin_bp.post("/retry-pipeline")
def trigger_retry() -> Response:
    """Re-run the post-call pipeline for all queued failed entries.

    Returns a JSON summary:
      {"retried": N, "succeeded": N, "failed": N, "failed_entries": [...]}
    """
    from flask import current_app
    try:
        from bookapt.pipeline_retry_queue import retry_all
        result = retry_all(current_app._get_current_object())  # type: ignore[attr-defined]
        status = 200 if result["failed"] == 0 else 207  # 207 Multi-Status = partial success
        return jsonify(result), status
    except Exception as exc:
        LOGGER.error("Error running pipeline retry: %s", exc)
        return jsonify({"error": str(exc)}), 500
