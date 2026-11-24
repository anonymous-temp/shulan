"""Close owned async streams before returning control to their caller."""

from contextlib import asynccontextmanager


@asynccontextmanager
async def owned_stream(iterator):
    try:
        yield iterator
    finally:
        close = getattr(iterator, 'aclose', None)
        if close is not None:
            await close()
