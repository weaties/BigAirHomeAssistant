"""Plain TCP relay for the Mac copy (#105): BIND:PORT -> TARGET:PORT, forever.

    python deploy/mac/relay.py 100.a.b.c 8123 100.x.y.z

Bytes only, so HA's websocket and login work unchanged, and HA sees the Mac's
tailnet address as the client. Stdlib asyncio rather than socat, so it runs under
the Python the macOS firewall already allows.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            writer.close()


async def main(bind: str, port: int, target: str) -> None:
    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            tr, tw = await asyncio.wait_for(asyncio.open_connection(target, port), 10)
        except (OSError, TimeoutError) as e:
            print(f"relay {port}: {target} unreachable: {e}", file=sys.stderr, flush=True)
            cw.close()
            return
        await asyncio.gather(_pipe(cr, tw), _pipe(tr, cw))

    server = await asyncio.start_server(handle, bind, port, reuse_address=True)
    print(f"relay {bind}:{port} -> {target}:{port}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3]))
