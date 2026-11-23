"""System event recording (database + structured log)."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Severity, SystemEvent

log = get_logger("events")

_LEVELS = {
    Severity.DEBUG: logging.DEBUG,
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}


def record_event(session: Session, severity: Severity, component: str, event_type: str,
                 message: str, *, job_id: str | None = None,
                 recording_id: str | None = None) -> SystemEvent:
    ev = SystemEvent(
        severity=severity.value,
        component=component,
        event_type=event_type,
        message=message[:4000],
        related_job_id=job_id,
        related_recording_id=recording_id,
    )
    session.add(ev)
    log.log(_LEVELS[severity], message, extra={
        "component": component, "event_type": event_type,
        "job_id": job_id, "recording_id": recording_id,
    })
    return ev
