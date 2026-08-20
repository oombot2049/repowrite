from __future__ import annotations

import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any


class ProviderErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission_denied"
    NOT_FOUND = "not_found"
    RATE_LIMIT = "rate_limited"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    SERVER = "server_error"
    CONTEXT_OVERFLOW = "context_overflow"
    CONTENT_POLICY = "content_policy"
    PROTOCOL = "protocol_error"
    CANCELLED = "cancelled"
    BUDGET = "budget_exceeded"
    CIRCUIT_OPEN = "circuit_open"
    UNKNOWN = "provider_unknown"


@dataclass(frozen=True)
class ProviderError:
    code: str
    category: ProviderErrorCategory
    retryable: bool
    safe_to_retry_before_output: bool
    public_message: str
    diagnostic_summary: str
    http_status: int | None = None
    provider_code: str | None = None
    retry_after_ms: int | None = None
    provider_request_id: str | None = None


class ProviderProtocolError(RuntimeError):
    def __init__(self, message: str, *, code: str = "provider_protocol_error") -> None:
        super().__init__(message)
        self.code = code


class ProviderTimeoutError(TimeoutError):
    def __init__(self, phase: str) -> None:
        super().__init__(f"provider {phase} timeout")
        self.phase = phase


class ProviderUpstreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.request_id = request_id


class ProviderBudgetExceeded(RuntimeError):
    pass


class ProviderCircuitOpen(RuntimeError):
    pass


class ProviderCallFailed(RuntimeError):
    def __init__(
        self,
        error: ProviderError,
        *,
        partial_output: bool = False,
        call_id: str | None = None,
    ) -> None:
        super().__init__(error.public_message)
        self.error = error
        self.partial_output = partial_output
        self.call_id = call_id


_SECRET_RE = re.compile(
    r"(?i)(authorization|api[-_ ]?key|token|secret)\s*[:=]\s*[^\s,;]+|sk-[A-Za-z0-9_-]{12,}"
)


def _redact(value: object) -> str:
    text = str(value)
    text = _SECRET_RE.sub("<redacted>", text)
    return text[:1000]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _retry_after_ms(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0, int(float(str(raw))) * 1000)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw)).timestamp()
            return max(0, int((target - time.time()) * 1000))
        except (TypeError, ValueError, OverflowError):
            return None


def _retry_after_message_ms(message: str) -> int | None:
    match = re.search(
        r"(?:try again|retry)\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s|seconds?)\b",
        message,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = float(match.group(1))
    if match.group(2).lower() == "ms":
        return max(0, int(value))
    return max(0, int(value * 1000))


def _provider_payload(exc: BaseException) -> tuple[str | None, str | None]:
    body = getattr(exc, "body", None)
    error = _field(body, "error", body)
    if error:
        code = _field(error, "code") or _field(error, "type")
        message = _field(error, "message")
        return (str(code) if code else None, str(message) if message else None)
    return None, None


def _looks_like_context_overflow(code: str | None, message: str) -> bool:
    haystack = f"{code or ''} {message}".lower()
    return any(
        marker in haystack
        for marker in (
            "context_length",
            "context window",
            "maximum context",
            "too many tokens",
            "context overflow",
        )
    )


def _looks_like_policy(code: str | None, message: str) -> bool:
    haystack = f"{code or ''} {message}".lower()
    return any(
        marker in haystack for marker in ("content_policy", "safety", "moderation")
    )


def _looks_like_rate_limit(code: str | None, message: str) -> bool:
    haystack = f"{code or ''} {message}".lower()
    return any(
        marker in haystack
        for marker in (
            "rate_limit",
            "rate limit",
            "too many requests",
            "tokens per min",
            "requests per min",
        )
    )


def classify_provider_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, ProviderCallFailed):
        return exc.error
    diagnostic = _redact(exc)
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "http_status", None)
    try:
        http_status = int(status) if status is not None else None
    except (TypeError, ValueError):
        http_status = None
    provider_code, provider_message = _provider_payload(exc)
    provider_code = provider_code or getattr(exc, "code", None)
    request_id = getattr(exc, "request_id", None)
    message = provider_message or diagnostic
    class_name = type(exc).__name__

    def result(
        code: str,
        category: ProviderErrorCategory,
        public: str,
        *,
        retryable: bool = False,
        safe: bool = False,
    ) -> ProviderError:
        return ProviderError(
            code=code,
            category=category,
            retryable=retryable,
            safe_to_retry_before_output=safe,
            public_message=public,
            diagnostic_summary=diagnostic,
            http_status=http_status,
            provider_code=str(provider_code) if provider_code else None,
            retry_after_ms=_retry_after_ms(exc)
            or _retry_after_message_ms(message),
            provider_request_id=str(request_id) if request_id else None,
        )

    if isinstance(exc, ProviderBudgetExceeded):
        return result(
            "provider_budget_exceeded",
            ProviderErrorCategory.BUDGET,
            "This Run has reached its configured model budget.",
        )
    if isinstance(exc, ProviderCircuitOpen):
        return result(
            "provider_circuit_open",
            ProviderErrorCategory.CIRCUIT_OPEN,
            "The configured model endpoint is temporarily unavailable.",
        )
    if isinstance(exc, ProviderTimeoutError):
        return result(
            f"provider_{exc.phase}_timeout",
            ProviderErrorCategory.TIMEOUT,
            f"The model provider timed out during {exc.phase}.",
            retryable=True,
            safe=True,
        )
    if isinstance(exc, ProviderProtocolError):
        return result(
            exc.code,
            ProviderErrorCategory.PROTOCOL,
            "The model provider returned an incomplete or invalid stream.",
            retryable=True,
            safe=True,
        )
    if isinstance(exc, ProviderUpstreamError):
        if _looks_like_rate_limit(provider_code, message):
            return result(
                "provider_rate_limited",
                ProviderErrorCategory.RATE_LIMIT,
                "The model provider is rate limiting requests.",
                retryable=True,
                safe=True,
            )
        if _looks_like_context_overflow(provider_code, message):
            return result(
                "provider_context_overflow",
                ProviderErrorCategory.CONTEXT_OVERFLOW,
                "The model context is too large for the configured model.",
            )
        if _looks_like_policy(provider_code, message):
            return result(
                "provider_content_policy",
                ProviderErrorCategory.CONTENT_POLICY,
                "The model provider declined this request under its content policy.",
            )
        return result(
            "provider_upstream_failed",
            ProviderErrorCategory.SERVER,
            "The model provider failed to complete the response.",
            retryable=True,
            safe=True,
        )

    if _looks_like_rate_limit(provider_code, message):
        return result(
            "provider_rate_limited",
            ProviderErrorCategory.RATE_LIMIT,
            "The model provider is rate limiting requests.",
            retryable=True,
            safe=True,
        )
    if _looks_like_context_overflow(provider_code, message):
        return result(
            "provider_context_overflow",
            ProviderErrorCategory.CONTEXT_OVERFLOW,
            "The model context is too large for the configured model.",
        )
    if _looks_like_policy(provider_code, message):
        return result(
            "provider_content_policy",
            ProviderErrorCategory.CONTENT_POLICY,
            "The model provider declined this request under its content policy.",
        )
    if http_status == 400 or class_name == "BadRequestError":
        return result(
            "provider_invalid_request",
            ProviderErrorCategory.INVALID_REQUEST,
            "The model request is not compatible with the selected model or transport.",
        )
    if http_status == 401 or class_name == "AuthenticationError":
        return result(
            "provider_authentication_failed",
            ProviderErrorCategory.AUTHENTICATION,
            "The model provider rejected the configured credentials.",
        )
    if http_status == 403 or class_name == "PermissionDeniedError":
        return result(
            "provider_permission_denied",
            ProviderErrorCategory.PERMISSION,
            "The configured account cannot use this model or endpoint.",
        )
    if http_status == 404 or class_name == "NotFoundError":
        return result(
            "provider_not_found",
            ProviderErrorCategory.NOT_FOUND,
            "The configured model or endpoint was not found.",
        )
    if http_status == 429 or class_name == "RateLimitError":
        return result(
            "provider_rate_limited",
            ProviderErrorCategory.RATE_LIMIT,
            "The model provider is rate limiting requests.",
            retryable=True,
            safe=True,
        )
    if (
        http_status is not None
        and http_status >= 500
        or class_name == "InternalServerError"
    ):
        return result(
            "provider_server_error",
            ProviderErrorCategory.SERVER,
            "The model provider is temporarily unavailable.",
            retryable=True,
            safe=True,
        )
    if class_name in {
        "APITimeoutError",
        "TimeoutException",
        "ReadTimeout",
        "ConnectTimeout",
    } or isinstance(exc, TimeoutError):
        return result(
            "provider_timeout",
            ProviderErrorCategory.TIMEOUT,
            "The model provider timed out.",
            retryable=True,
            safe=True,
        )
    if class_name in {
        "APIConnectionError",
        "ConnectError",
        "NetworkError",
    } or isinstance(exc, ConnectionError):
        return result(
            "provider_connection_error",
            ProviderErrorCategory.CONNECTION,
            "NexaPilot could not connect to the model provider.",
            retryable=True,
            safe=True,
        )
    if http_status is not None and 400 <= http_status < 500:
        return result(
            "provider_invalid_request",
            ProviderErrorCategory.INVALID_REQUEST,
            "The model provider rejected the request.",
        )
    return result(
        "provider_unknown_error",
        ProviderErrorCategory.UNKNOWN,
        "The model provider returned an unexpected error.",
    )
