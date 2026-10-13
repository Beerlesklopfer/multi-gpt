#!/usr/bin/python3 -I
"""mgpt-relay: Positivliste für ssh/telnet außerhalb des Arbeitscontainers (8w).

Läuft in einem eigenen Relay-Container der Sitzung (Image wie der
Arbeitscontainer, ``/usr/local/bin/mgpt-relay``). Der Arbeitscontainer hängt nur
an einem internen Podman-Netz ohne Gateway und ohne DNS; erreichbar ist dort
allein dieses Relay. Es nimmt auf ``BASE+i`` Verbindungen an und leitet sie fest
an das i-te freigegebene Ziel weiter. Andere Ziele gibt es nicht, und fällt das
Relay aus, ist der Weg zu (fail closed). Namen löst das Relay auf, nicht der
Arbeitscontainer.

Aufruf: ``mgpt-relay --base 20000 host:port [host:port …]``
"""

from __future__ import annotations

import argparse
import asyncio
import sys

IDLE_SECONDS = 3600


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE_SECONDS)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


def _handler(host: str, port: int):
    async def handle(reader, writer):
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        except (TimeoutError, OSError):
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    return handle


def parse_target(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"Ungültiges Ziel: {text}")
    return host, int(port)


async def serve(base: int, targets: list[tuple[str, int]], bind: str = "0.0.0.0") -> None:
    servers = []
    for index, (host, port) in enumerate(targets):
        servers.append(await asyncio.start_server(_handler(host, port), bind, base + index))
    await asyncio.gather(*(server.serve_forever() for server in servers))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mgpt-relay")
    parser.add_argument("--base", type=int, default=20000)
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args(argv)
    try:
        targets = [parse_target(t) for t in args.targets]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    asyncio.run(serve(args.base, targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
