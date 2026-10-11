"""Uso real calculado a partir dos registros (nada estimado ou inventado)."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.supabase_client import get_service_client


def _count(table: str, org_id: str, since: str, *, ts_column: str = "created_at", **filters: Any) -> int:
    query = (
        get_service_client().table(table).select("id", count="exact")
        .eq("organization_id", org_id).gte(ts_column, since)
    )
    for column, value in filters.items():
        query = query.eq(column, value)
    result = query.limit(1).execute()
    return int(result.count or 0)


def summary(org_id: str, days: int = 30) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    since_24h = (now - timedelta(hours=24)).isoformat()
    return {
        "period_days": days,
        "api_requests": _count("api_request_logs", org_id, since),
        "api_requests_24h": _count("api_request_logs", org_id, since_24h),
        "messages_sent": _count("messages", org_id, since, direction="outbound"),
        "messages_received": _count("messages", org_id, since, direction="inbound"),
        "messages_failed": _count("messages", org_id, since, status="failed"),
    }


def daily_messages(org_id: str, days: int = 14) -> list[dict[str, Any]]:
    """Mensagens por dia (UTC) e direção nos últimos ``days`` dias."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        get_service_client().table("messages").select("direction, created_at")
        .eq("organization_id", org_id).gte("created_at", start.isoformat())
        .order("created_at", desc=True).limit(10000).execute().data
    )
    counter: Counter[tuple[str, str]] = Counter()
    for row in rows:
        counter[(str(row.get("created_at", ""))[:10], row.get("direction") or "")] += 1
    out = []
    for i in range(days):
        day = (start + timedelta(days=i)).date().isoformat()
        out.append({"date": day, "inbound": counter[(day, "inbound")], "outbound": counter[(day, "outbound")]})
    return out
