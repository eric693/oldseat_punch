"""Database connection utilities with connection pooling for performance."""
import hashlib

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

# Connection pool: min 2 connections always open, up to 10 under load.
# Connections are recycled after 5 minutes idle (max_idle).
_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None and DATABASE_URL:
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=2,
            max_size=10,
            kwargs={'row_factory': dict_row},
            open=False,          # open lazily on first use
        )
        _pool.open(wait=True, timeout=10)
    return _pool


def get_db():
    """Return a psycopg connection from the pool (context manager supported)."""
    pool = _get_pool()
    if pool is not None:
        return pool.connection()          # returns a PooledConnection context manager
    # Fallback: direct connection when pool unavailable (e.g., no DATABASE_URL)
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()
