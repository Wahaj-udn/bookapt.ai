"""voxlayer — self-sufficient calling / transcription / analysis / result layer.

This package is a generalized extraction of the Carecaller pipeline
(place call -> record -> transcribe -> normalize -> extract -> aggregate),
stripped of healthcare-specific logic and re-shaped for BookAppt.ai's
appointment-negotiation use case.

It is designed to be dropped into a larger project (dashboard + calendar
layer + negotiation-payload builder) with a narrow, explicit interface:

    Inbound  -> models.BookingRequest   (what the caller layer needs to start/resume a call)
    Outbound -> models.CallResult        (what this layer hands back after a call)

Nothing in this package imports or assumes anything about calendars,
dashboards, or UI. See README.md in this folder for the full contract.
"""

from .models import (
    BookingRequest,
    CallResult,
    NegotiationOutcome,
    SlotWindow,
)

__all__ = [
    "BookingRequest",
    "CallResult",
    "NegotiationOutcome",
    "SlotWindow",
]
