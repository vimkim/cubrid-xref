"""XDG SQLite WAL cache for cubrid-xref lookup results."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cubrid_xref.models import CacheMeta

DEFAULT_TTL_HOURS = 24.0
DEFAULT_TTL_SECONDS = DEFAULT_TTL_HOURS * 3600.0
CACHE_SCHEMA_VERSION = 1

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    variant TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    fetched_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (kind, key, variant)
);
"""


def format_utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_cache_path(cache_dir: str | None = None) -> Path:
    if cache_dir:
        base = Path(cache_dir).expanduser()
    else:
        env_dir = os.environ.get("CUBRID_XREF_CACHE_DIR", "").strip()
        if env_dir:
            base = Path(env_dir).expanduser()
        else:
            xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
            xdg_path = Path(xdg).expanduser() if xdg else (Path.home() / ".cache")
            base = xdg_path / "cubrid-xref"
    return base / "cache.sqlite3"


def default_ttl_hours() -> float:
    raw = os.environ.get("CUBRID_XREF_CACHE_TTL_HOURS", "").strip()
    if not raw:
        return DEFAULT_TTL_HOURS
    try:
        val = float(raw)
        return val if val >= 0 else DEFAULT_TTL_HOURS
    except ValueError:
        return DEFAULT_TTL_HOURS


class XrefCache:
    """SQLite WAL cache storing PR and JIRA lookup payloads with 24h TTL."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_cache_path()

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        if not create and not self.path.exists():
            return None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        if create:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(_CREATE_SQL)
            conn.commit()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        return conn

    def get(
        self,
        kind: str,
        key: str,
        variant: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        allow_stale: bool = False,
        now: float | None = None,
    ) -> tuple[dict[str, Any], CacheMeta] | None:
        conn = self._connect(create=False)
        if conn is None:
            return None
        current = time.time() if now is None else float(now)
        try:
            row = conn.execute(
                "SELECT schema_version, fetched_at, payload_json "
                "FROM entries WHERE kind = ? AND key = ? AND variant = ?",
                (kind, key, variant),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()

        if not row:
            return None
        schema_ver, fetched_at, payload_json = row
        if int(schema_ver) != CACHE_SCHEMA_VERSION:
            return None

        age = max(0.0, current - float(fetched_at))
        stale = age > ttl_seconds
        if stale and not allow_stale:
            return None

        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return None

        meta = CacheMeta(
            hit=True,
            fetched_at=format_utc_iso(float(fetched_at)),
            age_seconds=round(age, 1),
            ttl_seconds=float(ttl_seconds),
            stale=stale,
            cache_path=str(self.path),
            source="cache_stale" if stale else "cache",
        )
        return payload, meta

    def put(
        self,
        kind: str,
        key: str,
        variant: str,
        payload: dict[str, Any],
        *,
        fetched_at: float | None = None,
    ) -> None:
        self.put_many([(kind, key, variant, payload)], fetched_at=fetched_at)

    def put_many(
        self,
        items: list[tuple[str, str, str, dict[str, Any]]],
        *,
        fetched_at: float | None = None,
    ) -> None:
        if not items:
            return
        ts = time.time() if fetched_at is None else float(fetched_at)
        conn = self._connect(create=True)
        assert conn is not None
        try:
            rows = [
                (
                    kind,
                    key,
                    variant,
                    CACHE_SCHEMA_VERSION,
                    ts,
                    json.dumps(payload, ensure_ascii=False),
                )
                for kind, key, variant, payload in items
            ]
            with conn:
                conn.executemany(
                    "INSERT INTO entries "
                    "(kind, key, variant, schema_version, fetched_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(kind, key, variant) DO UPDATE SET "
                    "schema_version = excluded.schema_version, "
                    "fetched_at = excluded.fetched_at, "
                    "payload_json = excluded.payload_json",
                    rows,
                )
        finally:
            conn.close()

    def stats(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        exists = self.path.exists()
        size_bytes = self.path.stat().st_size if exists else 0
        if not exists:
            return {
                "path": str(self.path),
                "exists": False,
                "size_bytes": 0,
                "total_entries": 0,
                "fresh_entries": 0,
                "expired_entries": 0,
                "by_kind": {"pr": 0, "jira": 0},
                "oldest_fetched_at": None,
                "newest_fetched_at": None,
            }

        current = time.time() if now is None else float(now)
        cutoff = current - ttl_seconds
        conn = self._connect(create=False)
        assert conn is not None
        try:
            total, fresh, oldest, newest = conn.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN fetched_at >= ? THEN 1 ELSE 0 END), 0), "
                "MIN(fetched_at), MAX(fetched_at) "
                "FROM entries",
                (cutoff,),
            ).fetchone()
            by_kind_rows = conn.execute(
                "SELECT kind, COUNT(*) FROM entries GROUP BY kind"
            ).fetchall()
        except sqlite3.Error:
            return {
                "path": str(self.path),
                "exists": True,
                "size_bytes": size_bytes,
                "total_entries": 0,
                "fresh_entries": 0,
                "expired_entries": 0,
                "by_kind": {"pr": 0, "jira": 0},
                "oldest_fetched_at": None,
                "newest_fetched_at": None,
            }
        finally:
            conn.close()

        by_kind = {"pr": 0, "jira": 0}
        for k, cnt in by_kind_rows:
            by_kind[str(k)] = int(cnt)
        total_int = int(total or 0)
        fresh_int = int(fresh or 0)
        return {
            "path": str(self.path),
            "exists": True,
            "size_bytes": size_bytes,
            "total_entries": total_int,
            "fresh_entries": fresh_int,
            "expired_entries": max(0, total_int - fresh_int),
            "by_kind": by_kind,
            "oldest_fetched_at": format_utc_iso(float(oldest)) if oldest is not None else None,
            "newest_fetched_at": format_utc_iso(float(newest)) if newest is not None else None,
        }

    def clear(
        self,
        *,
        expired_only: bool = False,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not self.path.exists():
            return {"path": str(self.path), "removed": 0, "remaining": 0}

        current = time.time() if now is None else float(now)
        cutoff = current - ttl_seconds
        conn = self._connect(create=False)
        assert conn is not None
        try:
            with conn:
                if expired_only:
                    cur = conn.execute("DELETE FROM entries WHERE fetched_at < ?", (cutoff,))
                else:
                    cur = conn.execute("DELETE FROM entries")
                removed = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            remaining = int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            if not expired_only:
                conn.execute("VACUUM")
        finally:
            conn.close()

        return {"path": str(self.path), "removed": removed, "remaining": remaining}
