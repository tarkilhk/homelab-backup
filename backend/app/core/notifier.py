"""Email notifier using SMTP environment variables.

Environment variables (read at send-time):
- SMTP_HOST (required)
- SMTP_PORT (optional; default 587)
- SMTP_USER (optional)
- SMTP_PASS (optional)
- SMTP_STARTTLS (optional; default "true")
- SMTP_FROM (required)
- SMTP_TO (required; comma-separated list)
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import List


def _get_bool(env_value: str | None, default: bool) -> bool:
    if env_value is None:
        return default
    return env_value.strip().lower() in {"1", "true", "yes", "on"}


def send_failure_email(subject: str, body: str) -> None:
    """Send a simple plaintext email. Failures are swallowed (log-only)."""
    host = os.getenv("SMTP_HOST")
    from_addr = os.getenv("SMTP_FROM")
    to_addrs_raw = os.getenv("SMTP_TO")

    if not host or not from_addr or not to_addrs_raw:
        # Missing config; do nothing
        return

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    use_starttls = _get_bool(os.getenv("SMTP_STARTTLS"), True)

    to_addrs: List[str] = [addr.strip() for addr in to_addrs_raw.split(",") if addr.strip()]
    if not to_addrs:
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)

    try:
        with smtplib.SMTP(host=host, port=port, timeout=15) as smtp:
            if use_starttls:
                smtp.starttls()
            if user:
                smtp.login(user, password or "")
            smtp.send_message(msg)
    except Exception:
        # Intentionally ignore to not break job flow
        pass


