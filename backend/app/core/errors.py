"""Application error types with stable error codes."""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict | None = None,
                 code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class UnsupportedMedia(AppError):
    status_code = 415
    code = "unsupported_media"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class InsufficientStorage(AppError):
    status_code = 507
    code = "insufficient_storage"


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"


# ---- Job execution errors -------------------------------------------------

class JobError(Exception):
    """Base for job failures. ``permanent`` errors are never retried."""

    permanent = False


class TransientJobError(JobError):
    permanent = False


class PermanentJobError(JobError):
    permanent = True


# ---- Storage (M1) ---------------------------------------------------------

class StorageOffline(AppError):
    """A storage location is not usable right now (unplugged, wrong drive, wrong
    filesystem, missing marker). Never a reason to fall back to another location."""

    status_code = 503
    code = "storage_offline"


class StorageOfflineJobError(TransientJobError):
    """Raised inside a job when a location it needs is offline. The worker defers the job
    without consuming an attempt."""

    permanent = False


# ---- Rights (M2) ----------------------------------------------------------

class RightsRefused(AppError):
    """The action is not authorized by a current rights record (fail-closed). ``details``
    carries the stable ``reason`` code."""

    status_code = 403
    code = "rights_refused"


class RightsRefusedJobError(PermanentJobError):
    """A rights-gated job is not (or no longer) authorized. Never retried automatically:
    once the rights are fixed the owner retries it."""

    permanent = True
