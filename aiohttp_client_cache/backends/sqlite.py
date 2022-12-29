import asyncio
import sqlite3
from contextlib import asynccontextmanager
from os import makedirs
from os.path import abspath, basename, dirname, expanduser, isabs, join
from pathlib import Path
from tempfile import gettempdir
from typing import AsyncIterable, AsyncIterator, Optional, Union

import aiosqlite

from aiohttp_client_cache.backends import BaseCache, CacheBackend, ResponseOrKey, get_valid_kwargs
from aiohttp_client_cache.signatures import extend_init_signature, sqlite_template


@extend_init_signature(CacheBackend, sqlite_template)
class SQLiteBackend(CacheBackend):
    """Async cache backend for `SQLite <https://www.sqlite.org>`_
    (requires `aiosqlite <https://aiosqlite.omnilib.dev>`_)

    The path to the database file will be ``<cache_name>`` (or ``<cache_name>.sqlite`` if no file
    extension is specified)

    Args:
        cache_name: Database filename
    use_temp: Store database in a temp directory (e.g., ``/tmp/http_cache.sqlite``).
        Note: if ``cache_name`` is an absolute path, this option will be ignored.
    fast_save: Increas cache write performance, but with the possibility of data loss. See
        `pragma: synchronous <http://www.sqlite.org/pragma.html#pragma_synchronous>`_ for details.
    """

    def __init__(
        self,
        cache_name: str = 'aiohttp-cache',
        use_temp: bool = False,
        fast_save: bool = False,
        **kwargs,
    ):
        super().__init__(cache_name=cache_name, **kwargs)
        self.responses = SQLitePickleCache(
            cache_name, 'responses', use_temp=use_temp, fast_save=fast_save, **kwargs
        )
        self.redirects = SQLiteCache(cache_name, 'redirects', use_temp=use_temp, **kwargs)

    async def close(self):
        await self.responses.close()


class SQLiteCache(BaseCache):
    """An async interface for caching objects in a SQLite database.

    Example:

        >>> # Store data in two tables under the 'testdb' database
        >>> d1 = SQLiteCache('testdb', 'table1')
        >>> d2 = SQLiteCache('testdb', 'table2')

    Args:
        filename: Database filename
        table_name: Table name
        use_temp: Store database in a temp directory (e.g., ``/tmp/http_cache.sqlite``).
            Note: if ``cache_name`` is an absolute path, this option will be ignored.
        kwargs: Additional keyword arguments for :py:func:`sqlite3.connect`
    """

    def __init__(
        self,
        filename: str,
        table_name: str = 'aiohttp-cache',
        use_temp: bool = False,
        fast_save: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.connection_kwargs = get_valid_kwargs(sqlite_template, kwargs)
        self.fast_save = fast_save
        self.filename = _get_cache_filename(filename, use_temp)
        self.table_name = table_name

        self._bulk_commit = False
        self._connection: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def get_connection(self, commit: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            if self._connection is None:
                self._connection = await aiosqlite.connect(self.filename, **self.connection_kwargs)
                await self._init_db()
        yield self._connection
        if commit and not self._bulk_commit:
            await self._connection.commit()

    async def _init_db(self):
        """Initialize the database, if it hasn't already been"""
        if self.fast_save:
            await self._connection.execute('PRAGMA synchronous = 0;')
        await self._connection.execute(
            f'CREATE TABLE IF NOT EXISTS `{self.table_name}` (key PRIMARY KEY, value)'
        )
        return self._connection

    @asynccontextmanager
    async def bulk_commit(self):
        """Contextmanager to more efficiently write a large number of records at once

        Example:

            >>> cache = SQLiteCache('test')
            >>> async with cache.bulk_commit():
            ...     for i in range(1000):
            ...         await cache.write(f'key_{i}', str(i))

        """
        async with self._lock:
            self._bulk_commit = True
        try:
            yield
            await self._connection.commit()
        finally:
            async with self._lock:
                self._bulk_commit = False

    async def clear(self):
        async with self.get_connection(commit=True) as db, self._lock:
            await db.execute(f'DROP TABLE `{self.table_name}`')
            await db.execute('VACUUM')
            await self._init_db()

    async def close(self):
        """Close any open connections"""
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    async def contains(self, key: str) -> bool:
        async with self.get_connection() as db:
            cursor = await db.execute(
                f'SELECT COUNT(*) FROM `{self.table_name}` WHERE key=?', (key,)
            )
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

    async def bulk_delete(self, keys: set):
        async with self.get_connection(commit=True) as db:
            placeholders = ", ".join("?" for _ in keys)
            await db.execute(
                f'DELETE FROM `{self.table_name}` WHERE key IN ({placeholders})', tuple(keys)
            )

    async def delete(self, key: str):
        async with self.get_connection(commit=True) as db:
            await db.execute(f'DELETE FROM `{self.table_name}` WHERE key=?', (key,))

    async def keys(self) -> AsyncIterable[str]:
        async with self.get_connection() as db:
            async with db.execute(f'SELECT key FROM `{self.table_name}`') as cursor:
                async for row in cursor:
                    yield row[0]

    async def read(self, key: str) -> ResponseOrKey:
        async with self.get_connection() as db:
            cursor = await db.execute(f'SELECT value FROM `{self.table_name}` WHERE key=?', (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def size(self) -> int:
        async with self.get_connection() as db:
            cursor = await db.execute(f'SELECT COUNT(key) FROM `{self.table_name}`')
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def values(self) -> AsyncIterable[ResponseOrKey]:
        async with self.get_connection() as db:
            async with db.execute(f'SELECT value FROM `{self.table_name}`') as cursor:
                async for row in cursor:
                    yield row[0]

    async def write(self, key: str, item: Union[ResponseOrKey, sqlite3.Binary]):
        async with self.get_connection(commit=True) as db:
            await db.execute(
                f'INSERT OR REPLACE INTO `{self.table_name}` (key,value) VALUES (?,?)',
                (key, item),
            )


class SQLitePickleCache(SQLiteCache):
    """Same as :py:class:`SqliteCache`, but pickles values before saving"""

    async def read(self, key: str) -> ResponseOrKey:
        return self.deserialize(await super().read(key))

    async def values(self) -> AsyncIterable[ResponseOrKey]:
        async with self.get_connection() as db:
            async with db.execute(f'select value from `{self.table_name}`') as cursor:
                async for row in cursor:
                    yield self.deserialize(row[0])

    async def write(self, key, item):
        await super().write(key, sqlite3.Binary(self.serialize(item)))


def _get_cache_filename(filename: Union[Path, str], use_temp: bool) -> str:
    """Get resolved path for database file"""
    # Save to a temp directory, if specified
    if use_temp and not isabs(filename):
        filename = join(gettempdir(), filename)

    # Expand relative and user paths (~/*), and add file extension if not specified
    filename = abspath(expanduser(str(filename)))
    if '.' not in basename(filename):
        filename += '.sqlite'

    # Make sure parent dirs exist
    makedirs(dirname(filename), exist_ok=True)
    return filename
