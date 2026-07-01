"""Storage backend for the graph package.

One backend: raw SQLite — FTS5 keyword search + sqlite-vec vectors + WAL for
concurrent readers. ``Database`` is the alias the graph code imports.
"""

from .base import BaseDatabase
from .raw_sqlite import RawSqliteDatabase

Database = RawSqliteDatabase

__all__ = [
    "Database",
    "BaseDatabase",
    "RawSqliteDatabase",
]
