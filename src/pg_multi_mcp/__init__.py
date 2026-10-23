import asyncio
import sys

from . import server


def main():
    """Main entry point for the package."""
    # Psycopg async on Windows is not compatible with the default ProactorEventLoop.
    # See: https://www.psycopg.org/psycopg3/docs/advanced/async.html#async
    if sys.platform == "win32":
        asyncio.run(server.main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.main())


__all__ = [
    "main",
    "server",
]
