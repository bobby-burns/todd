"""Previews: `http://localhost:PORT` in the agents' browser reaches the same port inside the sandbox, so agents can
open the app they're running there (a dev server, a static build) instead of deploying it somewhere just to look.

    browser: 127.0.0.1:PORT --socat--> api:BASE+i --(this relay)--> sandbox:BASE+i --(sandbox relay)--> 127.0.0.1:PORT

The sandbox stays off the browser's network: this relay in the API is the only path between them, and it only
forwards the preview ports (never the sandbox's exec API). The page keeps `localhost` as its origin, so dev servers
accept it and sign-in providers that allow localhost callbacks work.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from .config import config

log = logging.getLogger("todd.preview")

DEFAULT_PORTS = "3000,3001,4173,4321,5000,5173,8000,8080,8081,19006"
PORTS = [int(p) for p in os.getenv("PREVIEW_PORTS", DEFAULT_PORTS).replace(" ", "").split(",") if p.isdigit()]
BASE = int(os.getenv("PREVIEW_RELAY_BASE", "17000"))

_servers: list[asyncio.base_events.Server] = []


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        pass


async def relay(listen_host: str, listen_port: int, target_host: str, target_port: int) -> asyncio.base_events.Server:
    """A plain TCP relay: every connection to listen_host:listen_port is piped to target_host:target_port."""

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            tr, tw = await asyncio.wait_for(asyncio.open_connection(target_host, target_port), 5)
        except (OSError, asyncio.TimeoutError):
            w.close()
            return
        try:
            await asyncio.gather(_pipe(r, tw), _pipe(tr, w))
        finally:
            for x in (w, tw):
                try:
                    x.close()
                except Exception:  # noqa: BLE001
                    pass

    return await asyncio.start_server(handle, listen_host, listen_port)


def urls() -> list[str]:
    return [f"http://localhost:{p}" for p in PORTS]


async def start() -> None:
    """Listen on BASE+i for each preview port and forward to the sandbox's relay on the same port."""
    host = urlparse(config.sandbox_url).hostname or "sandbox"
    for i, _ in enumerate(PORTS):
        try:
            _servers.append(await relay("0.0.0.0", BASE + i, host, BASE + i))
        except OSError as e:  # e.g. running outside compose with the port taken: previews just won't work
            log.warning("preview relay on %s not started: %s", BASE + i, e)


async def stop() -> None:
    for s in _servers:
        s.close()
    _servers.clear()
