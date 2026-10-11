"""Filtros Jinja para exibição (datas em horário de Brasília, números)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Flask

BRT = timezone(timedelta(hours=-3), "BRT")


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def datetime_br(value, fmt: str = "%d/%m/%Y %H:%M") -> str:
    dt = _parse(value)
    return dt.astimezone(BRT).strftime(fmt) if dt else "—"


def time_ago(value) -> str:
    dt = _parse(value)
    if not dt:
        return "—"
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 60:
        return "agora"
    if seconds < 3600:
        return f"há {seconds // 60} min"
    if seconds < 86400:
        return f"há {seconds // 3600} h"
    if seconds < 86400 * 30:
        days = seconds // 86400
        return f"há {days} dia{'s' if days > 1 else ''}"
    return datetime_br(dt, "%d/%m/%Y")


def number_br(value) -> str:
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def phone_br(value) -> str:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    if digits.startswith("55") and len(digits) in (12, 13):
        local = digits[4:]
        return f"+55 {digits[2:4]} {local[:-4]}-{local[-4:]}"
    return f"+{digits}" if digits else "—"


def register_filters(app: Flask) -> None:
    app.add_template_filter(datetime_br, "dt")
    app.add_template_filter(time_ago, "ago")
    app.add_template_filter(number_br, "num")
    app.add_template_filter(phone_br, "phone")
