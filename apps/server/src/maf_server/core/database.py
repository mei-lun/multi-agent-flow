"""SQLite engine, PRAGMA setup, sessions, and write coordination.

This module owns WAL initialization and the process-wide write lock. It does
not contain module-specific SQL queries.
"""

from __future__ import annotations


class SQLiteWriteCoordinator:
    """Serializes short BEGIN IMMEDIATE transactions in the single server."""

