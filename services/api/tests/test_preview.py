"""Previews: localhost:PORT in the agents' browser reaches the same port in the sandbox, relayed through the API
(browser → API relay → sandbox relay → the dev server on the sandbox's localhost)."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
from pathlib import Path

from todd import preview

SERVER = Path(__file__).resolve().parents[2] / "sandbox" / "server.py"


def _free() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_the_relay_chain_reaches_a_dev_server_on_the_sandbox_localhost(loop, monkeypatch):
    dev, sandbox_side, api_side = _free(), _free(), _free()
    monkeypatch.setenv("PREVIEW_PORTS", f"{dev},7000")  # the sandbox's own exec port is never relayed
    monkeypatch.setenv("PREVIEW_RELAY_BASE", str(sandbox_side))
    spec = importlib.util.spec_from_file_location("todd_sandbox_server_preview", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert mod.PREVIEW_PORTS == [dev, 7000]

    async def go():
        async def app(r, w):  # a dev server that only listens on localhost
            req = await r.readuntil(b"\r\n\r\n")
            body = b"hello from the sandbox" if b"Host: localhost" in req else b"wrong host"
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            await w.drain()
            w.close()

        dev_server = await asyncio.start_server(app, "127.0.0.1", dev)
        async with mod.lifespan(mod.app):  # the sandbox's relays: sandbox_side+i → 127.0.0.1:port
            api_relay = await preview.relay("127.0.0.1", api_side, "127.0.0.1", sandbox_side)
            r, w = await asyncio.open_connection("127.0.0.1", api_side)  # what the browser's socat does
            w.write(f"GET / HTTP/1.1\r\nHost: localhost:{dev}\r\n\r\n".encode().replace(f":{dev}".encode(), b""))
            await w.drain()
            data = await asyncio.wait_for(r.read(), 5)
            w.close()
            api_relay.close()
            with socket.socket() as s:  # nothing listens for the exec port's slot
                assert s.connect_ex(("127.0.0.1", sandbox_side + 1)) != 0
        dev_server.close()
        assert data.endswith(b"hello from the sandbox")

    loop.run_until_complete(go())
    assert preview.urls()[0].startswith("http://localhost:")
