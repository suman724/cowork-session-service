"""Service-level exceptions mapped to standard error codes."""

from __future__ import annotations


class ServiceError(Exception):
    """Base for all session service errors."""

    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class SessionNotFoundError(ServiceError):
    """Session not found."""

    def __init__(self, session_id: str = "") -> None:
        msg = f"Session not found: {session_id}" if session_id else "Session not found"
        super().__init__(msg, code="SESSION_NOT_FOUND", status_code=404)


class ConflictError(ServiceError):
    """Invalid state transition."""

    def __init__(self, message: str = "Conflict") -> None:
        super().__init__(message, code="INVALID_REQUEST", status_code=409)


class PolicyBundleError(ServiceError):
    """Failed to fetch policy bundle from Policy Service."""

    def __init__(self, message: str = "Failed to fetch policy bundle") -> None:
        super().__init__(message, code="POLICY_BUNDLE_INVALID", status_code=502)


class DownstreamError(ServiceError):
    """Downstream service call failed."""

    def __init__(self, service: str, message: str = "") -> None:
        msg = f"Downstream {service} error: {message}" if message else f"Downstream {service} error"
        super().__init__(msg, code="INTERNAL_ERROR", status_code=502)


class IncompatibleError(ServiceError):
    """Client version is incompatible."""

    def __init__(self, message: str = "Client version incompatible") -> None:
        super().__init__(message, code="INVALID_REQUEST", status_code=400)


class ValidationError(ServiceError):
    """Request validation failed."""

    def __init__(self, message: str = "Invalid request") -> None:
        super().__init__(message, code="INVALID_REQUEST", status_code=400)
