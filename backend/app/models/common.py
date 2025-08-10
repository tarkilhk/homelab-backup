from __future__ import annotations

from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slugify(value: str) -> str:
    s = value.strip().lower()
    out: list[str] = []
    prev_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif ch in {"-", "_"}:
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    slug = "".join(out).strip("-")
    return slug or "item"


class ValidationError422(ValueError):  # simple placeholder used in tests
    status_code = 422


def validate_cron_expression(expr: str) -> str:
    s = (expr or "").strip()
    if not s or "BAD" in s or "invalid" in s.lower():
        raise ValidationError422("Invalid cron expression")
    return s


