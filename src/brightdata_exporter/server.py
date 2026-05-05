"""HTTP server — Prometheus /metrics + REST /api/* + health endpoints.

Uses the stdlib http.server (zero deps beyond prometheus_client). The
exporter+service is single-tenant and bursts at most a handful of
req/s during scrape; no async server is justified.

Health endpoints follow the convention of the Prometheus Foundation
exporters (blackbox_exporter, etc.): /healthz and /readyz return 200
as long as the HTTP server itself is responding. Upstream (Bright Data)
health is signalled via the `brightdata_up` metric and the
`brightdata_exporter_last_scrape_timestamp_seconds` gauge — NOT via
probe state. Tying readiness to upstream availability would cause
Prometheus to lose the very metric (`brightdata_up == 0`) needed to
diagnose the outage.
"""

from __future__ import annotations

import hmac
import http.server
import threading
import urllib.parse
from typing import TYPE_CHECKING

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

    from .service import BrightDataService

logger = structlog.get_logger(__name__)


def _verify_bearer(header_value: str | None, expected_token: str) -> bool:
    """Constant-time check of an ``Authorization: Bearer <token>`` header.

    Returns True when the supplied bearer token matches ``expected_token``.
    Uses ``hmac.compare_digest`` so attackers cannot mount a timing oracle
    against the token by measuring response latency.
    """
    if not header_value or not expected_token:
        return False
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1], expected_token)


_INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head><title>brightdata-exporter</title></head>
<body>
  <h1>Bright Data observability + FinOps service</h1>
  <p>Two endpoints families work side by side:</p>
  <h2>Prometheus exporter</h2>
  <ul>
    <li><a href="/metrics">/metrics</a> — Prometheus exposition (account
      balance, per-zone cost/traffic/requests for the configured rolling
      window, alerting metrics)</li>
  </ul>
  <h2>REST service (Grafana Infinity friendly)</h2>
  <ul>
    <li><code>GET /api/account</code> — account balance + status snapshot</li>
    <li><code>GET /api/zones?from=YYYY-MM-DD&amp;to=YYYY-MM-DD</code> —
      list of zones with cost/traffic/requests for that period</li>
    <li><code>GET /api/zones/{name}?from=&amp;to=</code> — full detail
      for a single zone</li>
  </ul>
  <h2>Health</h2>
  <ul>
    <li><a href="/healthz">/healthz</a> — liveness (200 while the HTTP server is up)</li>
    <li><a href="/readyz">/readyz</a> — readiness (200 while the HTTP server is up)</li>
  </ul>
  <p>Upstream (Bright Data) health is in the <code>brightdata_up</code>
    metric, not in these probe paths.</p>
  <p><a href="https://github.com/danielgines/brightdata-exporter">github.com/danielgines/brightdata-exporter</a></p>
</body>
</html>
"""


def make_handler(
    registry: CollectorRegistry,
    service: BrightDataService | None,
    api_auth_token: str = "",
) -> type[http.server.BaseHTTPRequestHandler]:
    """Construct a request handler bound to a metric registry + REST service.

    ``api_auth_token``: when non-empty, every ``/api/*`` request must carry
    an ``Authorization: Bearer <token>`` header matching this value.
    Mismatches return 401 with ``WWW-Authenticate: Bearer``. /metrics,
    /healthz, /readyz, and / are NEVER auth-gated — Prometheus scraping
    convention is unauth /metrics on a trusted network.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        # Silence the default access log; structlog logs scrapes elsewhere.
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(
            self,
            code: int,
            body: bytes,
            content_type: str,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # CORS — allow Grafana running on a different host to call the
            # JSON endpoints directly. Browsers require this for fetches
            # initiated from a different origin than the API server.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization")
            for hk, hv in extra_headers or ():
                self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(body)

        def _send_unauthorized(self) -> None:
            body = b'{"status":401,"error":"missing or invalid bearer token"}\n'
            self._send(
                401,
                body,
                "application/json; charset=utf-8",
                extra_headers=[
                    ("WWW-Authenticate", 'Bearer realm="brightdata-exporter"'),
                ],
            )

        def do_OPTIONS(self) -> None:
            self._send(204, b"", "text/plain")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}

            if path == "/metrics":
                payload = generate_latest(registry)
                self._send(200, payload, CONTENT_TYPE_LATEST)
                return
            if path in ("/healthz", "/readyz"):
                # Both endpoints return 200 while the HTTP server can respond.
                # Aligns with blackbox_exporter / prometheus-community exporter
                # conventions. Upstream health is exposed as `brightdata_up`.
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
                return
            if path.startswith("/api/"):
                if service is None:
                    self._send(
                        503,
                        b'{"error":"service not configured"}\n',
                        "application/json; charset=utf-8",
                    )
                    return
                # Bearer auth gate. When `api_auth_token` is empty the
                # endpoint is open (back-compat with v0.2.x); operators are
                # expected to set the token in production via
                # BRIGHTDATA_API_AUTH_TOKEN.
                if api_auth_token:
                    auth_header = self.headers.get("Authorization")
                    if not _verify_bearer(auth_header, api_auth_token):
                        self._send_unauthorized()
                        return
                status, body, content_type = service.handle(path, params)
                self._send(status, body, content_type)
                return
            if path in ("/", "/index.html"):
                self._send(200, _INDEX_HTML.encode(), "text/html; charset=utf-8")
                return
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    return Handler


class MetricsServer:
    """Threaded HTTP server with a controllable lifetime."""

    def __init__(
        self,
        host: str,
        port: int,
        registry: CollectorRegistry,
        service: BrightDataService | None = None,
        api_auth_token: str = "",
    ) -> None:
        handler_cls = make_handler(registry, service, api_auth_token=api_auth_token)
        self._httpd = http.server.ThreadingHTTPServer((host, port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="brightdata-exporter-http", daemon=True
        )
        self._host = host
        self._port = port

    def start(self) -> None:
        self._thread.start()
        logger.info("server.listening", host=self._host, port=self._port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        logger.info("server.stopped")
