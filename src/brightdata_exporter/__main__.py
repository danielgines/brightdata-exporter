"""Entry point — wires config, client, collector, server together.

Run with::

    python -m brightdata_exporter
    # or after `pip install`:
    brightdata-exporter
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from types import FrameType

import structlog
from pydantic import ValidationError

from . import __version__
from .cache import TTLCache
from .client import BrightDataAPIError, BrightDataClient
from .collector import Collector
from .config import Settings, load_settings
from .metrics import Metrics
from .ratelimit import RateLimiter
from .server import MetricsServer
from .service import BrightDataService


def _setup_logging(level: str, fmt: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        stream=sys.stderr,
    )
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def main() -> int:
    try:
        settings: Settings = load_settings()
    except ValidationError as exc:
        sys.stderr.write(f"configuration error:\n{exc}\n")
        return 2

    _setup_logging(settings.log_level, settings.log_format)
    log = structlog.get_logger("brightdata_exporter")
    log.info(
        "exporter.start",
        version=__version__,
        scrape_interval=settings.scrape_interval,
        period_days=settings.period_days,
        rate_limit_rps=settings.api_rate_limit_rps,
        listen=f"{settings.listen_host}:{settings.listen_port}",
        zones_filter=settings.zones_filter or "(all)",
    )

    metrics = Metrics()
    metrics.build_info.info({"version": __version__, "python": sys.version.split()[0]})
    metrics.up.set(0)  # set to 1 by the collector after the first ok scrape

    # Shared rate limiter — every upstream call (scheduled scrapes AND
    # on-demand /api/* requests) acquires from this single instance, so
    # they collectively respect Bright Data's 1 req/s/token limit.
    limiter = RateLimiter(settings.api_rate_limit_rps)

    client = BrightDataClient(
        token=settings.api_token,
        base_url=settings.api_base,
        timeout=settings.api_timeout_seconds,
        limiter=limiter,
    )

    collector = Collector(client=client, metrics=metrics, settings=settings, limiter=limiter)

    service: BrightDataService | None = None
    if settings.api_enabled:
        cache = TTLCache(
            ttl_seconds=settings.cache_ttl_seconds,
            max_size=settings.cache_max_size,
        )
        service = BrightDataService(client=client, cache=cache, settings=settings)
        log.info(
            "api.enabled",
            cache_ttl=settings.cache_ttl_seconds,
            auth_required=bool(settings.api_auth_token),
        )
        if not settings.api_auth_token:
            log.warning(
                "api.auth_disabled",
                reason="BRIGHTDATA_API_AUTH_TOKEN unset — /api/* is open",
                advice="Set the env var to a long random value in production",
            )
    else:
        log.info("api.disabled")

    server = MetricsServer(
        host=settings.listen_host,
        port=settings.listen_port,
        registry=metrics.registry,
        service=service,
        api_auth_token=settings.api_auth_token,
    )
    server.start()

    stop_event = threading.Event()

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("signal.received", signal=signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # First scrape happens immediately so /metrics has data on first pull.
    # If it fails the loop below retries on the configured interval — the
    # exporter stays "ready" (process up, /metrics serves what it has) and
    # `brightdata_up == 0` signals the upstream failure to Prometheus.
    try:
        collector.collect_once()
    except BrightDataAPIError as exc:
        log.error("scrape.first_failed", endpoint=exc.endpoint, status=exc.status)

    while not stop_event.wait(timeout=settings.scrape_interval):
        try:
            collector.collect_once()
        except Exception as exc:
            log.exception("scrape.unexpected", error=str(exc))
            metrics.up.set(0)
            time.sleep(1)

    server.stop()
    client.close()
    log.info("exporter.shutdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
