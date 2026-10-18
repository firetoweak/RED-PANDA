"""Cancellation-safe completion of blocking file operations."""
import asyncio


async def settled(function, *args):
    """A started file write must finish before cancellation is reported."""
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
            raise BaseExceptionGroup("file operation failed during cancellation", [cancelled, error]) from None
        raise
