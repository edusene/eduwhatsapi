"""Fakes em memória do cliente Supabase para testes (sem rede).

Implementa o subconjunto da API fluente do supabase-py usado pelo projeto:

    client.table("api_keys").insert(row).execute()
    client.table("t").select("cols").eq("a", 1).in_("b", [...]).order("c", desc=True)\\
        .limit(10).execute()
    client.table("t").update({...}).eq("id", x).execute()
    client.table("t").delete().eq("id", x).execute()

Resultados retornam objetos com `.data` (lista de dicts), como o supabase-py.
Suporta filtros aninhados simples de uma coluna (ex.: "organizations(...)").
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


class FakeResponse(SimpleNamespace):
    data: list[dict[str, Any]]


class FakeQuery:
    def __init__(self, table: "FakeTable"):
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: list[tuple[str, str, Any]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    # -- operações ---------------------------------------------------------
    def select(self, _columns: str = "*"):
        self._op = "select"
        return self

    def insert(self, rows: Any):
        self._op = "insert"
        self._payload = rows if isinstance(rows, list) else [rows]
        return self

    def upsert(self, rows: Any, **_kwargs):
        self._op = "upsert"
        self._payload = rows if isinstance(rows, list) else [rows]
        return self

    def update(self, values: dict):
        self._op = "update"
        self._payload = values
        return self

    def delete(self):
        self._op = "delete"
        return self

    # -- filtros ------------------------------------------------------------
    def eq(self, column: str, value: Any):
        self._filters.append(("eq", column, value))
        return self

    def neq(self, column: str, value: Any):
        self._filters.append(("neq", column, value))
        return self

    def in_(self, column: str, values: list):
        self._filters.append(("in", column, list(values)))
        return self

    def is_(self, column: str, value: Any):
        normalized = None if value in ("null", None) else value
        self._filters.append(("is", column, normalized))
        return self

    def gt(self, column: str, value: Any):
        self._filters.append(("gt", column, value))
        return self

    def lt(self, column: str, value: Any):
        self._filters.append(("lt", column, value))
        return self

    def order(self, column: str, desc: bool = False):
        self._order = (column, desc)
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def single(self):
        self._limit = 1
        return self

    # -- execução -----------------------------------------------------------
    def execute(self) -> FakeResponse:
        rows = self._table.rows
        if self._op == "insert":
            inserted = [self._table.insert_row(r) for r in self._payload]
            return FakeResponse(data=inserted)
        if self._op == "upsert":
            out = []
            for r in self._payload:
                match = next((x for x in rows if x.get("id") == r.get("id")), None)
                out.append(self._table.insert_row(r) if match is None else match)
            return FakeResponse(data=out)

        matched = [r for r in rows if self._matches(r)]
        if self._op == "delete":
            self._table.rows = [r for r in rows if not self._matches(r)]
            return FakeResponse(data=matched)
        if self._op == "update":
            for r in matched:
                r.update(copy.deepcopy(self._payload))
            return FakeResponse(data=copy.deepcopy(matched))

        result = matched
        if self._order:
            col, desc = self._order
            result = sorted(result, key=lambda r: (r.get(col) is None, r.get(col)),
                            reverse=desc)
        if self._limit is not None:
            result = result[: self._limit]
        return FakeResponse(data=copy.deepcopy(result))

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
            if op == "gt" and not (actual is not None and actual > val):
                return False
            if op == "lt" and not (actual is not None and actual < val):
                return False
        return True


class FakeTable:
    def __init__(self, name: str, store: dict[str, list]):
        self.name = name
        self._store = store

    @property
    def rows(self) -> list[dict]:
        return self._store.setdefault(self.name, [])

    @rows.setter
    def rows(self, value: list) -> None:
        self._store[self.name] = value

    def insert_row(self, row: dict) -> dict:
        record = copy.deepcopy(row)
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault(
            "created_at", datetime.now(timezone.utc).isoformat()
        )
        self.rows.append(record)
        return copy.deepcopy(record)


class FakeSession(SimpleNamespace):
    access_token: str | None = "fake-access-token"


class FakeAuth:
    """Mínimo para testar rotas de autenticação (signup/login/forgot/reset)."""

    def __init__(self):
        self._users: list[dict] = []
        self._last_error: str | None = None

    def sign_up(self, creds: dict) -> SimpleNamespace:
        email = creds["email"]
        if any(u["email"] == email for u in self._users):
            raise RuntimeError("User already registered")
        user = SimpleNamespace(id=uuid.uuid4().hex, email=email)
        self._users.append({"id": user.id, "email": email})
        return SimpleNamespace(user=user, session=FakeSession())

    def sign_in_with_password(self, creds: dict) -> SimpleNamespace:
        email = creds["email"]
        user = next((u for u in self._users if u["email"] == email), None)
        if not user:
            raise RuntimeError("Invalid login credentials")
        return SimpleNamespace(
            user=SimpleNamespace(id=user["id"], email=user["email"]),
            session=FakeSession(),
        )

    def reset_password_for_email(self, email: str, _options: dict) -> None:
        pass

    def update_user(self, attrs: dict) -> SimpleNamespace:
        if attrs.get("password") and len(attrs["password"]) < 10:
            raise RuntimeError("Password is too weak")
        return SimpleNamespace(user=SimpleNamespace(id="u", email="a@b.com"))


class FakeSupabaseClient:
    """Substitui o supabase.Client nos testes via override na app factory."""

    def __init__(self, seed: dict[str, list[dict]] | None = None):
        self._store: dict[str, list[dict]] = {k: [dict(r) for r in v]
                                              for k, v in (seed or {}).items()}
        self.auth = FakeAuth()

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(FakeTable(name, self._store))

    @property
    def store(self) -> dict[str, list[dict]]:
        return self._store
