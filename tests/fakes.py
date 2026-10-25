"""Fake em memória do cliente Supabase para testes (sem rede).

Implementa o subconjunto da API fluente do supabase-py usado pelo projeto,
incluindo restrições UNIQUE (para testar deduplicação/idempotência como no
PostgreSQL), ``count="exact"``, ``upsert(on_conflict=...)`` e as funções RPC
definidas em supabase/schema.sql.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


class FakeAPIError(Exception):
    """Imita postgrest.exceptions.APIError para violações de unicidade."""

    def __init__(self, table: str, columns: tuple[str, ...]):
        super().__init__(f"duplicate key value violates unique constraint (23505) on {table}{columns}")
        self.code = "23505"


# Restrições UNIQUE equivalentes às do schema.sql. Índices parciais recebem
# um predicado que decide se a linha participa da restrição.
UNIQUE: dict[str, list[tuple[tuple[str, ...], Any]]] = {
    "organizations": [(("slug",), None)],
    "organization_members": [(("organization_id", "user_id"), None)],
    "organization_invitations": [(("token_hash",), None)],
    "api_keys": [(("key_hash",), None)],
    "webhook_events": [(("dedupe_key",), None)],
    "idempotency_keys": [(("organization_id", "key", "endpoint"), None)],
    "contacts": [(("organization_id", "wa_id"), None)],
    "conversations": [(("connection_id", "contact_id"), None)],
    "messages": [(("connection_id", "wamid"), lambda r: r.get("wamid") is not None)],
    "meta_connections": [(("channel", "phone_number_id"),
                          lambda r: r.get("status") != "disconnected" and r.get("phone_number_id"))],
}
DEFAULTS: dict[str, dict[str, Any]] = {
    "organizations": {"plan": "api_chat", "message_retention_days": 90},
    "meta_connections": {"status": "pending", "channel": "whatsapp", "connection_type": "manual",
                         "webhook_subscribed": False, "registered": False, "granted_scopes": [],
                         "metadata": {}},
    "conversations": {"status": "open", "unread_count": 0},
    "webhook_endpoints": {"status": "active"},
    "webhook_deliveries": {"attempts": 0, "status": "pending"},
    "idempotency_keys": {"status": "completed"},
    "webhook_events": {"status": "processed"},
}
TIME_COLUMN = {"webhook_events": "received_at"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeResponse(SimpleNamespace):
    data: list[dict[str, Any]]
    count: int | None = None


class FakeQuery:
    def __init__(self, db: "FakeSupabaseClient", table: str):
        self._db = db
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: list[tuple[str, str, Any]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._offset = 0
        self._count = False
        self._on_conflict: tuple[str, ...] | None = None
        self._ignore_duplicates = False

    # -- operações ---------------------------------------------------------
    def select(self, _columns: str = "*", count: str | None = None):
        self._op = "select"
        self._count = count == "exact"
        return self

    def insert(self, rows: Any):
        self._op = "insert"
        self._payload = rows if isinstance(rows, list) else [rows]
        return self

    def upsert(self, rows: Any, on_conflict: str = "", ignore_duplicates: bool = False, **_kwargs):
        self._op = "upsert"
        self._payload = rows if isinstance(rows, list) else [rows]
        self._on_conflict = tuple(c.strip() for c in on_conflict.split(",") if c.strip()) or ("id",)
        self._ignore_duplicates = ignore_duplicates
        return self

    def update(self, values: dict):
        self._op = "update"
        self._payload = values
        return self

    def delete(self):
        self._op = "delete"
        return self

    # -- filtros ------------------------------------------------------------
    def _f(self, op: str, column: str, value: Any):
        self._filters.append((op, column, value))
        return self

    def eq(self, c, v): return self._f("eq", c, v)
    def neq(self, c, v): return self._f("neq", c, v)
    def gt(self, c, v): return self._f("gt", c, v)
    def gte(self, c, v): return self._f("gte", c, v)
    def lt(self, c, v): return self._f("lt", c, v)
    def lte(self, c, v): return self._f("lte", c, v)
    def in_(self, c, v): return self._f("in", c, list(v))

    def is_(self, column: str, value: Any):
        return self._f("is", column, None if value in ("null", None) else value)

    def order(self, column: str, desc: bool = False):
        self._order = (column, desc)
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def range(self, start: int, end: int):
        self._offset, self._limit = start, end - start + 1
        return self

    # -- execução -----------------------------------------------------------
    def execute(self) -> FakeResponse:
        db = self._db
        rows = db.rows(self._table)
        if self._op == "insert":
            return FakeResponse(data=[db.insert_row(self._table, r) for r in self._payload])
        if self._op == "upsert":
            out = []
            for r in self._payload:
                match = next((x for x in rows if all(x.get(c) == r.get(c) for c in self._on_conflict)), None)
                if match is None:
                    out.append(db.insert_row(self._table, r))
                elif not self._ignore_duplicates:
                    match.update(copy.deepcopy(r))
                    match["updated_at"] = _now()
                    out.append(copy.deepcopy(match))
            return FakeResponse(data=out)

        matched = [r for r in rows if self._matches(r)]
        if self._op == "delete":
            db.set_rows(self._table, [r for r in rows if not self._matches(r)])
            return FakeResponse(data=copy.deepcopy(matched))
        if self._op == "update":
            for r in matched:
                candidate = {**r, **copy.deepcopy(self._payload)}
                db.check_unique(self._table, candidate, ignore=r)
                r.update(copy.deepcopy(self._payload))
                r["updated_at"] = _now()
            return FakeResponse(data=copy.deepcopy(matched))

        result = matched
        if self._order:
            col, desc = self._order
            result = sorted(result, key=lambda r: (r.get(col) is None, r.get(col) or ""), reverse=desc)
        total = len(result)
        if self._limit is not None:
            result = result[self._offset: self._offset + self._limit]
        return FakeResponse(data=copy.deepcopy(result), count=total if self._count else None)

    def _matches(self, row: dict) -> bool:
        for op, col, val in self._filters:
            actual = row.get(col)
            if op == "eq" and actual != val:
                return False
            if op == "neq" and actual == val:
                return False
            if op == "in" and actual not in val:
                return False
            if op == "is" and ((actual is None) != (val is None)):
                return False
            if op in ("gt", "gte", "lt", "lte"):
                if actual is None:
                    return False
                if op == "gt" and not actual > val:
                    return False
                if op == "gte" and not actual >= val:
                    return False
                if op == "lt" and not actual < val:
                    return False
                if op == "lte" and not actual <= val:
                    return False
        return True


class FakeRPC:
    def __init__(self, db: "FakeSupabaseClient", name: str, params: dict):
        self._db, self._name, self._params = db, name, params

    def execute(self) -> FakeResponse:
        fn = getattr(self._db, f"_rpc_{self._name}", None)
        if fn is None:
            raise RuntimeError(f"RPC desconhecida no fake: {self._name}")
        return FakeResponse(data=fn(**self._params))


class FakeSession(SimpleNamespace):
    access_token: str | None = "fake-access-token"


class FakeAuth:
    """Mínimo para testar rotas de autenticação."""

    def __init__(self):
        self.users: list[dict] = []
        self.confirm_email = False
        self.tokens: dict[str, dict] = {}

    def sign_up(self, creds: dict) -> SimpleNamespace:
        email = creds["email"]
        if any(u["email"] == email for u in self.users):
            raise RuntimeError("User already registered")
        user = {"id": str(uuid.uuid4()), "email": email, "password": creds["password"]}
        self.users.append(user)
        session = None if self.confirm_email else FakeSession()
        return SimpleNamespace(user=SimpleNamespace(id=user["id"], email=email), session=session)

    def sign_in_with_password(self, creds: dict) -> SimpleNamespace:
        user = next((u for u in self.users if u["email"] == creds["email"]), None)
        if not user or user["password"] != creds["password"]:
            raise RuntimeError("Invalid login credentials")
        return SimpleNamespace(user=SimpleNamespace(id=user["id"], email=user["email"]), session=FakeSession())

    def get_user(self, token: str) -> SimpleNamespace:
        user = self.tokens.get(token)
        if not user:
            raise RuntimeError("invalid JWT")
        return SimpleNamespace(user=SimpleNamespace(id=user["id"], email=user["email"]))

    def reset_password_for_email(self, email: str, _options: dict) -> None:
        return None

    def update_user(self, attrs: dict) -> SimpleNamespace:
        return SimpleNamespace(user=SimpleNamespace(id="u", email="a@b.com"))


class FakeSupabaseClient:
    """Substitui o supabase.Client nos testes via override na app factory."""

    def __init__(self):
        self._store: dict[str, list[dict]] = {}
        self.auth = FakeAuth()

    # -- tabelas -------------------------------------------------------------
    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rpc(self, name: str, params: dict | None = None) -> FakeRPC:
        return FakeRPC(self, name, params or {})

    @property
    def store(self) -> dict[str, list[dict]]:
        return self._store

    def rows(self, table: str) -> list[dict]:
        return self._store.setdefault(table, [])

    def set_rows(self, table: str, rows: list[dict]) -> None:
        self._store[table] = rows

    def check_unique(self, table: str, candidate: dict, ignore: dict | None = None) -> None:
        for columns, predicate in UNIQUE.get(table, []):
            if predicate is not None and not predicate(candidate):
                continue
            for other in self.rows(table):
                if other is ignore:
                    continue
                if predicate is not None and not predicate(other):
                    continue
                if all(other.get(c) == candidate.get(c) for c in columns):
                    raise FakeAPIError(table, columns)

    def insert_row(self, table: str, row: dict) -> dict:
        record = {**DEFAULTS.get(table, {}), **copy.deepcopy(row)}
        record.setdefault("id", str(uuid.uuid4()))
        now = _now()
        record.setdefault(TIME_COLUMN.get(table, "created_at"), now)
        record.setdefault("updated_at", now)
        self.check_unique(table, record)
        self.rows(table).append(record)
        return copy.deepcopy(record)

    # -- RPCs (espelham as funções de schema.sql) --------------------------------
    def _rpc_bump_conversation(self, p_conversation_id, p_preview, p_inbound, p_at):
        for conv in self.rows("conversations"):
            if conv["id"] == p_conversation_id:
                if p_inbound:
                    conv["unread_count"] = conv.get("unread_count", 0) + 1
                    conv["last_inbound_at"] = max(conv.get("last_inbound_at") or p_at, p_at)
                    conv["status"] = "open"
                conv["last_message_at"] = max(conv.get("last_message_at") or p_at, p_at)
                conv["last_message_preview"] = (p_preview or "")[:160]
        return []

    def _rpc_claim_due_webhook_deliveries(self, p_limit=25):
        now = _now()
        due = [d for d in self.rows("webhook_deliveries")
               if d.get("status") == "pending" and d.get("next_retry_at") and d["next_retry_at"] <= now
               and (not d.get("locked_until") or d["locked_until"] < now)]
        due.sort(key=lambda d: d["next_retry_at"])
        claimed = due[: max(1, min(p_limit, 100))]
        for d in claimed:
            d["locked_until"] = "9999-12-31T00:00:00+00:00"
        return copy.deepcopy(claimed)
