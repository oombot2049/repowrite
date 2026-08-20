from __future__ import annotations

from nexapilot.security.access import evaluate_request_security, security_headers


def _decision(**overrides):
    values = {
        "method": "GET",
        "scheme": "http",
        "host_header": "127.0.0.1:4096",
        "client_host": "127.0.0.1",
        "origin": None,
        "sec_fetch_site": None,
    }
    values.update(overrides)
    return evaluate_request_security(**values)


def test_loopback_request_is_allowed() -> None:
    assert _decision().allowed
    assert _decision(client_host="::1", host_header="[::1]:4096").allowed
    assert _decision(client_host="::ffff:127.0.0.1").allowed


def test_remote_client_is_denied_even_with_local_host_header() -> None:
    decision = _decision(client_host="192.168.1.20")
    assert not decision.allowed
    assert decision.status_code == 403
    assert decision.error_code == "remote_access_denied"


def test_untrusted_host_is_rejected_before_origin_processing() -> None:
    decision = _decision(host_header="attacker.example")
    assert not decision.allowed
    assert decision.status_code == 400
    assert decision.error_code == "untrusted_host"


def test_state_changing_browser_request_must_be_same_origin() -> None:
    assert _decision(method="POST", origin="http://127.0.0.1:4096").allowed
    mismatch = _decision(method="POST", origin="https://evil.example")
    assert not mismatch.allowed
    assert mismatch.error_code == "origin_mismatch"
    wrong_port = _decision(method="POST", origin="http://127.0.0.1:5000")
    assert not wrong_port.allowed
    assert wrong_port.error_code == "origin_mismatch"


def test_cross_site_fetch_metadata_is_rejected_without_origin() -> None:
    decision = _decision(method="DELETE", sec_fetch_site="cross-site")
    assert not decision.allowed
    assert decision.error_code == "cross_site_request_denied"


def test_non_browser_local_cli_mutation_is_allowed_without_origin() -> None:
    assert _decision(method="PATCH", origin=None, sec_fetch_site=None).allowed


def test_testclient_transport_requires_both_synthetic_names() -> None:
    assert _decision(client_host="testclient", host_header="testserver").allowed
    denied = _decision(client_host="testclient", host_header="127.0.0.1:4096")
    assert not denied.allowed


def test_security_headers_are_strict() -> None:
    headers = security_headers()
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "microphone=()" in headers["Permissions-Policy"]
