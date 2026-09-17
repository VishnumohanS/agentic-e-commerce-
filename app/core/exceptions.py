"""Typed exceptions shared by both agents.

Every exception carries a stable machine-readable `code` so that failures can
be recorded in the audit ledger and mapped to HTTP status codes consistently.
"""

from __future__ import annotations

from typing import Any


class AgenticCommerceError(Exception):
    """Base class for all domain errors."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# --- Configuration -------------------------------------------------------


class ConfigurationError(AgenticCommerceError):
    code = "configuration_error"
    http_status = 500


# --- AP2 mandate ---------------------------------------------------------


class MandateError(AgenticCommerceError):
    code = "mandate_error"
    http_status = 403


class MandateSignatureError(MandateError):
    code = "mandate_invalid_signature"


class MandateExpiredError(MandateError):
    code = "mandate_expired"


class MandateScopeError(MandateError):
    code = "mandate_scope_violation"


class MandateCurrencyError(MandateError):
    code = "mandate_currency_mismatch"


class BudgetExceededError(MandateError):
    code = "mandate_budget_exceeded"


class MandateReplayError(MandateError):
    code = "mandate_replay_detected"


class MandateMalformedError(MandateError):
    code = "mandate_malformed"
    http_status = 400


# --- Catalog / inventory -------------------------------------------------


class ProductNotFoundError(AgenticCommerceError):
    code = "product_not_found"
    http_status = 404


class InsufficientInventoryError(AgenticCommerceError):
    code = "insufficient_inventory"
    http_status = 409


class ReservationNotFoundError(AgenticCommerceError):
    code = "reservation_not_found"
    http_status = 404


# --- Payments ------------------------------------------------------------


class PaymentError(AgenticCommerceError):
    code = "payment_error"
    http_status = 502


class PaymentSignatureError(PaymentError):
    code = "payment_signature_invalid"
    http_status = 400


class PaymentNotCapturedError(PaymentError):
    code = "payment_not_captured"
    http_status = 402


class PaymentAmountMismatchError(PaymentError):
    code = "payment_amount_mismatch"
    http_status = 400


# --- AI ------------------------------------------------------------------


class AIProviderError(AgenticCommerceError):
    code = "ai_provider_error"
    http_status = 502


# --- Ledger --------------------------------------------------------------


class LedgerError(AgenticCommerceError):
    code = "ledger_error"
    http_status = 500


class LedgerIntegrityError(LedgerError):
    code = "ledger_integrity_violation"


# --- Protocols -----------------------------------------------------------


class ProtocolError(AgenticCommerceError):
    code = "protocol_error"
    http_status = 400


class AgentUnavailableError(AgenticCommerceError):
    code = "agent_unavailable"
    http_status = 503


class AuthorizationError(AgenticCommerceError):
    code = "unauthorized"
    http_status = 401


class PurchaseRejectedError(AgenticCommerceError):
    """Raised when the buyer agent deliberately refuses to complete a purchase."""

    code = "purchase_rejected"
    http_status = 422


# --- remote error reconstruction -----------------------------------------

_ERROR_REGISTRY: dict[str, type[AgenticCommerceError]] = {}


def _register(cls: type[AgenticCommerceError]) -> None:
    _ERROR_REGISTRY[cls.code] = cls
    for subclass in cls.__subclasses__():
        _register(subclass)


_register(AgenticCommerceError)


def map_remote_error(
    code: str, message: str, details: dict[str, Any] | None = None
) -> AgenticCommerceError:
    """Rebuild a typed exception from an error payload received over A2A."""
    cls = _ERROR_REGISTRY.get(code, AgenticCommerceError)
    return cls(message, details=details or {})
