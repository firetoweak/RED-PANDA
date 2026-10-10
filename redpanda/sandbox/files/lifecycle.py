"""File-operation ownership and cancellation-safe completion."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3

async def settled(function, *args):
    """A native lifecycle must settle before cancellation releases ownership."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
        except BaseException as error:
            raise BaseExceptionGroup("sandbox operation failed during cancellation", [cancelled, error]) from None
        raise

@asynccontextmanager
async def _sqlite_lock(path: Path, *, shared: bool = False):
    def acquire():
        connection = sqlite3.connect(path, timeout=0)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS lock_guard (id INTEGER PRIMARY KEY)")
            connection.execute("BEGIN" if shared else "BEGIN EXCLUSIVE")
            # A deferred transaction holds no read lock until its first read.
            if shared:
                connection.execute("SELECT id FROM lock_guard LIMIT 1").fetchone()
            return connection
        except BaseException:
            connection.close()
            raise
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 300
    while True:
        try:
            connection = acquire()
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or loop.time() >= deadline:
                raise
            # Never occupy an executor thread while waiting for another task
            # to release a lock. Only lock acquisition is retried.
            await asyncio.sleep(0.01)
        else:
            break
    try:
        yield
    finally:
        connection.close()

