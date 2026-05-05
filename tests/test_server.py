"""Tests for the HTTP server boundary, especially the bearer-auth gate
on /api/* added in v0.2.6.

These tests run a real ``MetricsServer`` bound to an ephemeral port
(127.0.0.1:0 — kernel picks free port) and exercise it via
``urllib.request``. Live socket > monkey-patched handler — catches
real header / status / WWW-Authenticate behavior the way Grafana sees it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import pytest
from prometheus_client import CollectorRegistry, Gauge

from brightdata_exporter.cache import TTLCache
from brightdata_exporter.client import BrightDataClient
from brightdata_exporter.config import load_settings
from brightdata_exporter.server import MetricsServer, _verify_bearer
from brightdata_exporter.service import BrightDataService

# ---------------------------------------------------------------------------
# _verify_bearer — pure function, no HTTP needed
# ---------------------------------------------------------------------------


def test_verify_bearer_accepts_correct_token():
    assert _verify_bearer("Bearer secret-token-123", "secret-token-123") is True


def test_verify_bearer_rejects_wrong_token():
    assert _verify_bearer("Bearer wrong", "right") is False


def test_verify_bearer_rejects_missing_header():
    assert _verify_bearer(None, "right") is False
    assert _verify_bearer("", "right") is False


def test_verify_bearer_rejects_wrong_scheme():
    """Basic / Token / etc must NOT pass — only Bearer."""
    assert _verify_bearer("Basic dXNlcjpwYXNz", "any") is False
    assert _verify_bearer("Token secret", "secret") is False


def test_verify_bearer_is_case_insensitive_on_scheme():
    """RFC 6750: scheme is case-insensitive."""
    assert _verify_bearer("bearer xyz", "xyz") is True
    assert _verify_bearer("BEARER xyz", "xyz") is True


def test_verify_bearer_rejects_empty_expected():
    """An empty server-side token must reject everything (prevents the
    'unset config means accept any header' trap)."""
    assert _verify_bearer("Bearer anything", "") is False


# ---------------------------------------------------------------------------
# End-to-end via real HTTP server
# ---------------------------------------------------------------------------


@pytest.fixture
def stack(httpx_mock):
    """Spin up a MetricsServer bound to an ephemeral port + return helpers."""
    httpx_mock.add_response(
        url=re.compile(r".*?/customer/balance"),
        json={"balance": 100, "credit": 0, "prepayment": 200, "pending_costs": 0},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/status$"),
        json={
            "status": "active",
            "customer": "t",
            "can_make_requests": True,
            "auth_fail_reason": "",
            "ip": "10.0.0.1",
        },
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*?/zone/get_all_zones"),
        json=[],
        is_reusable=True,
        is_optional=True,
    )

    registry = CollectorRegistry()
    Gauge("test_smoke", "smoke", registry=registry).set(1)
    settings = load_settings(api_token="t", api_rate_limit_rps=1000.0)
    client = BrightDataClient(token="t")
    cache = TTLCache(ttl_seconds=60)
    service = BrightDataService(client=client, cache=cache, settings=settings)

    def make(api_auth_token: str = ""):
        srv = MetricsServer(
            host="127.0.0.1",
            port=0,  # ephemeral
            registry=registry,
            service=service,
            api_auth_token=api_auth_token,
        )
        # ThreadingHTTPServer.server_address holds the bound (host, port)
        # after construction even before start().
        srv.start()
        host, port = srv._httpd.server_address
        return srv, f"http://{host}:{port}"

    yield make
    client.close()


def _get(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as exc:
        return exc  # has .status, .headers, .read()


def test_metrics_endpoint_unauthenticated_when_token_is_set(stack):
    """Even with auth token configured, /metrics must stay open —
    Prometheus scrape contract assumes unauth /metrics on trusted nets."""
    srv, base = stack(api_auth_token="secret")
    try:
        resp = _get(f"{base}/metrics")
        assert resp.status == 200
        assert b"test_smoke" in resp.read()
    finally:
        srv.stop()


def test_health_endpoints_unauthenticated_when_token_is_set(stack):
    srv, base = stack(api_auth_token="secret")
    try:
        for path in ("/healthz", "/readyz"):
            resp = _get(f"{base}{path}")
            assert resp.status == 200
    finally:
        srv.stop()


def test_api_open_when_token_unset(stack):
    """Backward compat — when BRIGHTDATA_API_AUTH_TOKEN is unset (default),
    /api/* accepts requests without an Authorization header."""
    srv, base = stack(api_auth_token="")
    try:
        resp = _get(f"{base}/api/account")
        assert resp.status == 200
        payload = json.loads(resp.read())
        assert "balance" in payload
    finally:
        srv.stop()


def test_api_rejects_missing_auth_when_token_set(stack):
    srv, base = stack(api_auth_token="server-secret")
    try:
        resp = _get(f"{base}/api/account")
        assert resp.status == 401
        assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer")
        body = json.loads(resp.read())
        assert body["status"] == 401
        assert "bearer" in body["error"].lower()
    finally:
        srv.stop()


def test_api_rejects_wrong_token(stack):
    srv, base = stack(api_auth_token="server-secret")
    try:
        resp = _get(
            f"{base}/api/account",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status == 401
    finally:
        srv.stop()


def test_api_accepts_correct_token(stack):
    srv, base = stack(api_auth_token="server-secret")
    try:
        resp = _get(
            f"{base}/api/account",
            headers={"Authorization": "Bearer server-secret"},
        )
        assert resp.status == 200
        assert b"balance" in resp.read()
    finally:
        srv.stop()


def test_api_rejects_basic_auth_even_with_correct_value(stack):
    """A client sending the token via Basic auth must NOT be accepted —
    only Bearer scheme is honored."""
    srv, base = stack(api_auth_token="server-secret")
    try:
        # Base64('server-secret:') is effectively the secret as a Basic creds.
        # Wrong scheme regardless of value.
        resp = _get(
            f"{base}/api/account",
            headers={"Authorization": "Basic c2VydmVyLXNlY3JldDo="},
        )
        assert resp.status == 401
    finally:
        srv.stop()


def test_options_preflight_works_without_auth(stack):
    """CORS preflight must succeed without the bearer token — browsers
    send OPTIONS without Authorization, and Grafana relies on this to
    cross-origin call /api/*."""
    srv, base = stack(api_auth_token="server-secret")
    try:
        req = urllib.request.Request(
            f"{base}/api/zones",
            method="OPTIONS",
            headers={
                "Origin": "https://grafana.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        resp = urllib.request.urlopen(req, timeout=2)
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"
        # Authorization must be in allow-headers so browsers will let
        # the followup GET include it.
        assert "Authorization" in resp.headers.get("Access-Control-Allow-Headers", "")
    finally:
        srv.stop()


def test_index_page_unauthenticated(stack):
    srv, base = stack(api_auth_token="server-secret")
    try:
        resp = _get(f"{base}/")
        assert resp.status == 200
        assert b"brightdata-exporter" in resp.read().lower()
    finally:
        srv.stop()


def test_unknown_path_returns_404(stack):
    srv, base = stack(api_auth_token="")
    try:
        resp = _get(f"{base}/nope")
        assert resp.status == 404
    finally:
        srv.stop()
