"""Relay a visitor's websocket to a service the app can reach and they can't.

For apps that front a local companion (e.g. go2rtc) and must check who may
use it first. From a raw ASGI websocket app (a Channels route)::

    async def live(scope, receive, send):
        await receive()  # websocket.connect
        if not allowed(scope):
            await send({"type": "websocket.close", "code": 4403})
            return
        await wsrelay.relay(receive, send, "ws://127.0.0.1:1984/api/ws?src=cam")

Needs the ``websockets`` package (the ``runtime`` extra and the hub have it).
This module imports nothing until :func:`relay` is called.
"""
import asyncio
import logging

__all__ = ["relay"]

log = logging.getLogger(__name__)

MAX_SIZE = 16 * 1024 * 1024


def _connect(url, headers, subprotocols, open_timeout, max_size):
    try:
        from websockets.asyncio.client import connect
        return connect(url, additional_headers=headers or None, subprotocols=subprotocols or None,
                       open_timeout=open_timeout, max_size=max_size, compression=None)
    except ImportError:  # websockets < 13
        import websockets
        return websockets.connect(url, extra_headers=headers or None,
                                  subprotocols=subprotocols or None,
                                  open_timeout=open_timeout, max_size=max_size, compression=None)


async def relay(receive, send, url, *, headers=(), subprotocols=(), open_timeout=5,
                max_size=MAX_SIZE):
    """Connect to ``url``, accept the visitor and pass messages both ways
    until either side closes. Call after receiving ``websocket.connect``.

    Returns False (after closing the visitor with 1013) if ``url`` refused.
    """
    try:
        upstream = await _connect(url, list(headers), list(subprotocols), open_timeout, max_size)
    except Exception as exc:
        log.info("websocket relay to %s refused: %s", url.split("?", 1)[0], exc)
        await send({"type": "websocket.close", "code": 1013})
        return False
    try:
        await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})

        async def to_upstream():
            while True:
                msg = await receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])

        async def to_visitor():
            try:
                async for data in upstream:
                    key = "bytes" if isinstance(data, bytes) else "text"
                    await send({"type": "websocket.send", key: data})
            except Exception as exc:  # upstream dropped without a close frame
                log.debug("websocket relay upstream ended: %s", exc)
            await send({"type": "websocket.close", "code": 1000})

        tasks = [asyncio.ensure_future(to_upstream()), asyncio.ensure_future(to_visitor())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        log.debug("websocket relay ended: %s", exc)
    finally:
        await upstream.close()
    return True
