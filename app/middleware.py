from __future__ import annotations

import logging
import time


logger = logging.getLogger("app.request")


class RequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        method = request.method
        path = request.get_full_path()
        remote_addr = request.META.get("REMOTE_ADDR", "-")

        try:
            response = self.get_response(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request_failed method=%s path=%s remote=%s duration_ms=%.2f",
                method,
                path,
                remote_addr,
                elapsed_ms,
            )
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "request_complete method=%s path=%s status=%s remote=%s duration_ms=%.2f",
            method,
            path,
            getattr(response, "status_code", "-"),
            remote_addr,
            elapsed_ms,
        )
        return response
