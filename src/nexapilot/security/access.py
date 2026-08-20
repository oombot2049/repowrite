from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class RequestSecurityDecision:
    allowed: bool
    status_code: int = 200
    error_code: str = ""
    detail: str = ""


def _hostname(host_header: str) -> str:
    value = host_header.strip()
    if not value:
        return ""
    try:
        return (urlsplit(f"//{value}").hostname or "").lower()
    except ValueError:
        return ""


def _origin_authority(origin: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return parsed.hostname.lower(), port
    except ValueError:
        return None


def _host_authority(host_header: str, scheme: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlsplit(f"//{host_header.strip()}")
        if not parsed.hostname:
            return None
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80
        return parsed.hostname.lower(), port
    except ValueError:
        return None


def _is_loopback_client(client_host: str | None) -> bool:
    if not client_host:
        return False
    try:
        address = ipaddress.ip_address(client_host)
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _is_test_transport(client_host: str | None, host_header: str) -> bool:
    """Allow Starlette's in-process TestClient without weakening socket traffic."""
    return client_host == "testclient" and _hostname(host_header) == "testserver"


def evaluate_request_security(
    *,
    method: str,
    scheme: str,
    host_header: str,
    client_host: str | None,
    origin: str | None,
    sec_fetch_site: str | None,
) -> RequestSecurityDecision:
    """Enforce the local-only product boundary and same-origin mutations."""
    host = _hostname(host_header)
    is_test = _is_test_transport(client_host, host_header)
    if host not in _LOCAL_HOSTS and not is_test:
        return RequestSecurityDecision(
            allowed=False,
            status_code=400,
            error_code="untrusted_host",
            detail="The Host header is not allowed by the local console.",
        )

    if not _is_loopback_client(client_host) and not is_test:
        return RequestSecurityDecision(
            allowed=False,
            status_code=403,
            error_code="remote_access_denied",
            detail="NexaPilot Console only accepts local connections.",
        )

    if method.upper() in _SAFE_METHODS:
        return RequestSecurityDecision(allowed=True)

    if sec_fetch_site and sec_fetch_site.lower() == "cross-site":
        return RequestSecurityDecision(
            allowed=False,
            status_code=403,
            error_code="cross_site_request_denied",
            detail="Cross-site state-changing requests are not allowed.",
        )

    if origin is None:
        # CLI and other non-browser local clients do not normally send Origin.
        return RequestSecurityDecision(allowed=True)

    request_authority = _host_authority(host_header, scheme)
    origin_authority = _origin_authority(origin)
    if origin_authority is None or request_authority != origin_authority:
        return RequestSecurityDecision(
            allowed=False,
            status_code=403,
            error_code="origin_mismatch",
            detail="The request Origin does not match the local console.",
        )
    return RequestSecurityDecision(allowed=True)


def security_headers() -> dict[str, str]:
    return {
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
