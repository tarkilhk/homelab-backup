import logging

from app.core.logging import setup_logging


def test_setup_logging_suppresses_http_client_request_urls() -> None:
    setup_logging("DEBUG")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
