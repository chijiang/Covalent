"""Application-layer errors.

Services raise these instead of ``fastapi.HTTPException`` so the application
layer stays framework-independent. A FastAPI exception handler in the API layer
maps them to HTTP responses (see ``covalent.api.app``).
"""

from __future__ import annotations


class ApplicationError(Exception):
    """Base error raised by application services."""

    status_code = 500
    message: str

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(ApplicationError):
    status_code = 404


class ConflictError(ApplicationError):
    status_code = 409


class ForbiddenError(ApplicationError):
    status_code = 403


class UnauthorizedError(ApplicationError):
    status_code = 401


class InvalidInputError(ApplicationError):
    status_code = 400


class UnprocessableEntityError(ApplicationError):
    status_code = 422


class ServiceUnavailableError(ApplicationError):
    status_code = 503


class QuotaExceededError(ApplicationError):
    status_code = 429


class DelegateRunNotFoundError(NotFoundError): ...
class DelegateOwnershipError(ForbiddenError): ...
class DelegateTransitionError(ConflictError): ...
class DelegateConcurrentModificationError(ConflictError): ...
class DelegateDefinitionError(UnprocessableEntityError): ...
class DelegateQuotaError(QuotaExceededError): ...
class DelegateRunGoneError(ConflictError): ...
