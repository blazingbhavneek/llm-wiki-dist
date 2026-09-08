"""Encrypted local registry for external GROWI connections."""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field


class GrowiConnection(BaseModel):
    name: str
    url: str
    api_token: str = ""
    mongo_uri: str | None = None
    mode: str = "attach"
    root_path: str = "/"
    write_path: str = "/inbox"
    sync_cursor: str | None = None
    last_sync_at: str | None = None
    last_error: str | None = None
    created_at: str = ""


class GrowiPageIndex(BaseModel):
    name: str
    page_id: str
    revision_id: str
    path: str
    index_hash: str | None = None
    indexed_at: str | None = None


class ConnectionRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS growi_connections (
                    name TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    api_token_enc TEXT NOT NULL,
                    mongo_uri_enc TEXT,
                    mode TEXT NOT NULL DEFAULT 'attach',
                    root_path TEXT NOT NULL DEFAULT '/',
                    write_path TEXT NOT NULL DEFAULT '/inbox',
                    sync_cursor TEXT,
                    last_sync_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS growi_pages (
                    name TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    index_hash TEXT,
                    indexed_at TEXT,
                    PRIMARY KEY (name, page_id)
                );
                """
            )

    @staticmethod
    def _secret_key() -> bytes:
        value = os.environ.get("WIKI_SECRET_KEY", "").strip()
        if not value:
            raise RuntimeError(
                "WIKI_SECRET_KEY must be set before storing GROWI credentials"
            )
        try:
            Fernet(value.encode("ascii"))
            return value.encode("ascii")
        except Exception:
            # Accept a passphrase while still using Fernet for storage. This is
            # deterministic, and avoids silently falling back to plaintext.
            digest = hashlib.sha256(value.encode("utf-8")).digest()
            return base64.urlsafe_b64encode(digest)

    @classmethod
    def _fernet(cls) -> Fernet:
        return Fernet(cls._secret_key())

    @classmethod
    def _encrypt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    @classmethod
    def _decrypt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return cls._fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except Exception as exc:
            raise RuntimeError("could not decrypt GROWI registry secret") from exc

    def register(
        self,
        *,
        name: str,
        url: str,
        api_token: str,
        mongo_uri: str | None = None,
        mode: str = "attach",
        root_path: str = "/",
        write_path: str = "/inbox",
    ) -> GrowiConnection:
        if mode not in {"attach", "own"}:
            raise ValueError("mode must be attach or own")
        if not api_token:
            raise ValueError("api_token is required")
        now = datetime.now(timezone.utc).isoformat()
        connection = GrowiConnection(
            name=name,
            url=url.rstrip("/"),
            api_token=api_token,
            mongo_uri=mongo_uri,
            mode=mode,
            root_path=root_path or "/",
            write_path=write_path or "/inbox",
            created_at=now,
        )
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO growi_connections
                  (name,url,api_token_enc,mongo_uri_enc,mode,root_path,write_path,created_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                  url=excluded.url,
                  api_token_enc=excluded.api_token_enc,
                  mongo_uri_enc=excluded.mongo_uri_enc,
                  mode=excluded.mode,
                  root_path=excluded.root_path,
                  write_path=excluded.write_path
                """,
                (
                    connection.name,
                    connection.url,
                    self._encrypt(connection.api_token),
                    self._encrypt(connection.mongo_uri),
                    connection.mode,
                    connection.root_path,
                    connection.write_path,
                    connection.created_at,
                ),
            )
        return connection

    def get(self, name: str) -> GrowiConnection | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM growi_connections WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["api_token"] = self._decrypt(data.pop("api_token_enc")) or ""
        data["mongo_uri"] = self._decrypt(data.pop("mongo_uri_enc"))
        return GrowiConnection.model_validate(data)

    def list(self) -> list[GrowiConnection]:
        with self._lock, self._connect() as db:
            names = [row["name"] for row in db.execute("SELECT name FROM growi_connections ORDER BY name")]
        return [connection for name in names if (connection := self.get(name)) is not None]

    def update(self, name: str, **fields: Any) -> GrowiConnection:
        current = self.get(name)
        if current is None:
            raise KeyError(name)
        if not fields.get("api_token"):
            fields["api_token"] = current.api_token
        if "mongo_uri" not in fields:
            fields["mongo_uri"] = current.mongo_uri
        merged = current.model_copy(update=fields)
        return self.register(
            name=merged.name,
            url=merged.url,
            api_token=merged.api_token,
            mongo_uri=merged.mongo_uri,
            mode=merged.mode,
            root_path=merged.root_path,
            write_path=merged.write_path,
        )

    def delete(self, name: str) -> bool:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM growi_pages WHERE name=?", (name,))
            result = db.execute("DELETE FROM growi_connections WHERE name=?", (name,))
        return result.rowcount > 0

    def public(self, connection: GrowiConnection) -> dict[str, Any]:
        data = connection.model_dump()
        data["api_token"] = ""
        data["mongo_uri"] = "" if connection.mongo_uri else None
        data["has_token"] = bool(connection.api_token)
        return data

    def record_sync(
        self,
        name: str,
        *,
        cursor: str | None = None,
        synced_at: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE growi_connections SET sync_cursor=?, last_sync_at=?, last_error=? WHERE name=?",
                (cursor, synced_at, error, name),
            )

    def upsert_page(self, page: GrowiPageIndex) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO growi_pages(name,page_id,revision_id,path,index_hash,indexed_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(name,page_id) DO UPDATE SET
                  revision_id=excluded.revision_id,
                  path=excluded.path,
                  index_hash=excluded.index_hash,
                  indexed_at=excluded.indexed_at
                """,
                tuple(page.model_dump().values()),
            )

    def delete_page(self, name: str, page_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM growi_pages WHERE name=? AND page_id=?", (name, page_id))

    def pages(self, name: str) -> list[GrowiPageIndex]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM growi_pages WHERE name=? ORDER BY path", (name,)
            ).fetchall()
        return [GrowiPageIndex.model_validate(dict(row)) for row in rows]

    def page_count(self, name: str) -> int:
        with self._lock, self._connect() as db:
            return int(
                db.execute("SELECT count(*) FROM growi_pages WHERE name=?", (name,)).fetchone()[0]
            )
