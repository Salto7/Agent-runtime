"""Tiny SQLite table layer — the subset of the Django ORM the catalog uses.

The platform catalog (roles, skills, MCP servers, bindings) was written against
Django models. This keeps its call sites unchanged (``Model.objects.filter(...)``
/ ``update_or_create`` / ``bulk_create`` …) while storing rows in one stdlib
``sqlite3`` file, so the library has no Django dependency.

Point every model at a database with :func:`configure_store` (a file path, or
``":memory:"``); with nothing configured an in-memory database is used.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Iterable, Iterator

IntegrityError = sqlite3.IntegrityError


class _Db:
    def __init__(self) -> None:
        self.path = ":memory:"
        self.explicit = False  # False ⇒ resolve the path from the runtime config on first use
        self._conn: sqlite3.Connection | None = None
        self.lock = threading.RLock()
        self._depth = 0

    def reset(self) -> None:
        """Forget the current database (called when the runtime config changes)."""
        with self.lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self.explicit = False

    def configure(self, path: str | Path) -> None:
        with self.lock:
            if self.explicit and self._conn is not None and self.path == str(path):
                return  # already open on this file
            self.explicit = True
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self.path = str(path)
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def conn(self) -> sqlite3.Connection:
        with self.lock:
            if self._conn is None and not self.explicit:
                try:  # default: the platform workspace's catalog.sqlite3 (shared with PlatformCatalog)
                    from orchestrator.config import get_config

                    self.configure(Path(get_config().platform_workspace_dir) / "catalog.sqlite3")
                except Exception:  # noqa: BLE001 — no config yet: stay in memory
                    pass
            if self._conn is None:
                self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                self._conn.row_factory = sqlite3.Row
                # several worker processes share one catalog file: wait for the write lock, don't fail
                self._conn.execute("PRAGMA busy_timeout=30000")
                self._conn.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA synchronous=OFF")
                for model in Model._registry:
                    model._create_table(self._conn)
            return self._conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            conn = self.conn()
            outer = self._depth == 0
            if outer:
                conn.execute("BEGIN IMMEDIATE")   # take the write lock up front: a read→write upgrade would fail at once
            self._depth += 1   # only after BEGIN succeeded, or a failed BEGIN would leave every later atomic() "nested"
            try:
                yield
            except BaseException:
                if outer:
                    conn.execute("ROLLBACK")
                raise
            else:
                if outer:
                    try:
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.execute("ROLLBACK")   # a failed COMMIT (busy) must not leave the connection mid-transaction
                        raise
            finally:
                self._depth -= 1


DB = _Db()


def configure_store(path: str | Path) -> None:
    """Use ``path`` (or ``":memory:"``) as the catalog database."""
    DB.configure(path)


def reset_store() -> None:
    """Drop the open database so the next use re-resolves it from the (new) runtime config."""
    DB.reset()


def atomic():
    """``with atomic():`` — one transaction (re-entrant)."""
    return DB.transaction()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Field:
    def __init__(self, default: Any = None, *, json_: bool = False, kind: str = "TEXT") -> None:
        self.default = default
        self.json = json_
        self.kind = kind

    def make_default(self) -> Any:
        return self.default() if callable(self.default) else (
            json.loads(json.dumps(self.default)) if isinstance(self.default, (list, dict)) else self.default
        )


def text(default: Any = "") -> Field:
    return Field(default)


def js(default: Any = None) -> Field:
    return Field(default, json_=True)


def flag(default: bool = False) -> Field:
    return Field(default, kind="INTEGER")


def num(default: float = 0.0) -> Field:
    return Field(default, kind="REAL")


def integer(default: int = 0) -> Field:
    return Field(default, kind="INT")


class QuerySet:
    def __init__(self, model: type["Model"], where: list[tuple[str, str, Any]] | None = None,
                 order: list[str] | None = None) -> None:
        self.model = model
        self._where = list(where or [])
        self._order = list(order or [])

    # -- building -----------------------------------------------------------
    def _clone(self, **kw: Any) -> "QuerySet":
        qs = QuerySet(self.model, self._where, self._order)
        for key, val in kw.items():
            setattr(qs, key, val)
        return qs

    def filter(self, **lookups: Any) -> "QuerySet":
        extra = []
        for key, value in lookups.items():
            name, _, op = key.partition("__")
            extra.append((name, op or "exact", value))
        return self._clone(_where=[*self._where, *extra])

    def exclude(self, **lookups: Any) -> "QuerySet":
        extra = []
        for key, value in lookups.items():
            name, _, op = key.partition("__")
            extra.append((name, "not_" + (op or "exact"), value))
        return self._clone(_where=[*self._where, *extra])

    def all(self) -> "QuerySet":
        return self._clone()

    def order_by(self, *names: str) -> "QuerySet":
        return self._clone(_order=list(names))

    # -- compiling ----------------------------------------------------------
    def _sql(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for name, op, value in self._where:
            field = self.model._fields.get(name)
            if field is None:
                raise ValueError(f"{self.model.__name__} has no field {name!r}")
            neg = op.startswith("not_")
            base = op[4:] if neg else op
            if base == "exact":
                if value is None:
                    clause, p = f"{name} IS NULL", []
                else:
                    clause, p = f"{name} = ?", [self.model._to_db(name, value)]
            elif base == "in":
                vals = [self.model._to_db(name, v) for v in value]
                if not vals:
                    clause, p = "0", []
                else:
                    clause, p = f"{name} IN ({','.join('?' * len(vals))})", vals
            elif base == "isnull":
                clause, p = (f"{name} IS NULL" if value else f"{name} IS NOT NULL"), []
            else:
                raise ValueError(f"unsupported lookup {op!r}")
            clauses.append(f"NOT ({clause})" if neg else clause)
            params.extend(p)
        sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        return sql, params

    def _order_sql(self) -> str:
        names = self._order or list(self.model._ordering)
        if not names:
            return ""
        parts = [f"{n[1:]} DESC" if n.startswith("-") else n for n in names]
        return " ORDER BY " + ", ".join(parts)

    # -- reading ------------------------------------------------------------
    def _rows(self) -> list["Model"]:
        where, params = self._sql()
        with DB.lock:
            cur = DB.conn().execute(f"SELECT * FROM {self.model._table}{where}{self._order_sql()}", params)
            return [self.model._from_db(r) for r in cur.fetchall()]

    def __iter__(self) -> Iterator["Model"]:
        return iter(self._rows())

    def iterator(self) -> Iterator["Model"]:
        return iter(self._rows())

    def first(self) -> "Model | None":
        rows = self._rows()
        return rows[0] if rows else None

    def exists(self) -> bool:
        where, params = self._sql()
        with DB.lock:
            row = DB.conn().execute(f"SELECT 1 FROM {self.model._table}{where} LIMIT 1", params).fetchone()
        return row is not None

    def count(self) -> int:
        where, params = self._sql()
        with DB.lock:
            return int(DB.conn().execute(f"SELECT COUNT(*) FROM {self.model._table}{where}", params).fetchone()[0])

    def values_list(self, name: str, flat: bool = False) -> list[Any]:
        return [getattr(r, name) for r in self._rows()]

    # -- writing ------------------------------------------------------------
    def update(self, **values: Any) -> int:
        rows = self._rows()
        moves_pk = any(k in self.model.pk_fields for k in values)
        silent = set(values) <= set(getattr(self.model, "_silent_fields", ()))
        for row in rows:
            if moves_pk:
                row.delete()   # a changed primary key is a new row: drop the old one
            for key, val in values.items():
                setattr(row, key, val)
            row.save(touch=not silent)
        return len(rows)

    def delete(self) -> int:
        where, params = self._sql()
        with DB.lock:
            cur = DB.conn().execute(f"DELETE FROM {self.model._table}{where}", params)
            return cur.rowcount


class Manager:
    def __init__(self, model: type["Model"]) -> None:
        self.model = model

    def _qs(self) -> QuerySet:
        return QuerySet(self.model)

    def all(self) -> QuerySet:
        return self._qs()

    def filter(self, **kw: Any) -> QuerySet:
        return self._qs().filter(**kw)

    def exclude(self, **kw: Any) -> QuerySet:
        return self._qs().exclude(**kw)

    def exists(self) -> bool:
        return self._qs().exists()

    def count(self) -> int:
        return self._qs().count()

    def get_or_create(self, defaults: dict[str, Any] | None = None, **lookup: Any) -> tuple["Model", bool]:
        with atomic():
            row = self.filter(**lookup).first()
            if row is not None:
                return row, False
            row = self.model(**{**(defaults or {}), **lookup})
            row.save(force_insert=True)
            return row, True

    def update_or_create(self, defaults: dict[str, Any] | None = None, **lookup: Any) -> tuple["Model", bool]:
        with atomic():
            row = self.filter(**lookup).first()
            if row is None:
                row = self.model(**{**lookup, **(defaults or {})})
                row.save(force_insert=True)
                return row, True
            for key, val in (defaults or {}).items():
                setattr(row, key, val)
            row.save()
            return row, False

    def bulk_create(self, rows: Iterable["Model"]) -> list["Model"]:
        out = list(rows)
        with atomic():
            for row in out:
                row.save(force_insert=True)
        return out

    def latest_updated(self) -> float:
        with DB.lock:
            row = DB.conn().execute(f"SELECT MAX(updated_at) FROM {self.model._table}").fetchone()
        if not row or not row[0]:
            return 0.0
        try:
            return datetime.fromisoformat(row[0]).timestamp()
        except ValueError:
            return 0.0


class Model:
    """Declare fields as class attributes; set ``pk`` (tuple for composite keys)."""

    _registry: ClassVar[list[type["Model"]]] = []
    _fields: ClassVar[dict[str, Field]]
    _own_fields: ClassVar[dict[str, Field]] = {}
    _table: ClassVar[str]
    pk_fields: ClassVar[tuple[str, ...]] = ()
    _ordering: ClassVar[tuple[str, ...]] = ()
    objects: ClassVar[Manager]

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        abstract = cls.__name__.startswith("_")
        own = {n: v for n, v in vars(cls).items() if isinstance(v, Field)}
        cls._own_fields = own
        fields: dict[str, Field] = {}
        for base in reversed(cls.__mro__):
            fields.update(getattr(base, "_own_fields", {}) if base is not cls else own)
        fields["updated_at"] = Field("")
        cls._fields = fields
        cls._table = cls.__name__.lower()
        cls.objects = Manager(cls)
        if not abstract:
            Model._registry.append(cls)
            if DB._conn is not None:
                cls._create_table(DB._conn)
        for name in own:
            delattr(cls, name)

    def __init__(self, **values: Any) -> None:
        for name, field in self._fields.items():
            setattr(self, name, values.pop(name) if name in values else field.make_default())
        if values:
            raise TypeError(f"unknown fields for {type(self).__name__}: {sorted(values)}")

    # -- sql ----------------------------------------------------------------
    @classmethod
    def _create_table(cls, conn: sqlite3.Connection) -> None:
        cols = [f"{n} {f.kind}" for n, f in cls._fields.items()]
        pk = cls.pk_fields
        extra = f", PRIMARY KEY ({', '.join(pk)})" if pk else ""
        conn.execute(f"CREATE TABLE IF NOT EXISTS {cls._table} ({', '.join(cols)}{extra})")

    @classmethod
    def _to_db(cls, name: str, value: Any) -> Any:
        field = cls._fields[name]
        if field.json:
            return None if value is None else json.dumps(value)
        if field.kind == "INTEGER":
            return int(bool(value))
        if field.kind == "INT":
            return int(value or 0)
        return value

    @classmethod
    def _from_db(cls, row: sqlite3.Row) -> "Model":
        values: dict[str, Any] = {}
        for name, field in cls._fields.items():
            raw = row[name]
            if field.json:
                values[name] = None if raw is None else json.loads(raw)
            elif field.kind == "INTEGER":
                values[name] = bool(raw)
            else:
                values[name] = raw
        return cls(**values)

    def save(self, update_fields: Iterable[str] | None = None, force_insert: bool = False, touch: bool = True) -> None:
        del update_fields  # whole-row writes
        if touch:
            self.updated_at = _now()
        names = list(self._fields)
        vals = [self._to_db(n, getattr(self, n)) for n in names]
        verb = "INSERT" if force_insert else "INSERT OR REPLACE"
        with DB.lock:
            DB.conn().execute(
                f"{verb} INTO {self._table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", vals
            )

    def delete(self) -> None:
        where = " AND ".join(f"{n} = ?" for n in self.pk_fields)
        with DB.lock:
            DB.conn().execute(
                f"DELETE FROM {self._table} WHERE {where}",
                [self._to_db(n, getattr(self, n)) for n in self.pk_fields],
            )


def latest_stamp(model: type[Model]) -> float:
    """Newest ``updated_at`` in a table, as a timestamp (0.0 when empty)."""
    return model.objects.latest_updated()
