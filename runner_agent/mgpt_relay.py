#!/usr/bin/python3 -I
"""mgpt-relay: Positivliste für ssh/telnet und Proxy für Paketquellen (8w, M14).

Läuft in einem eigenen Relay-Container der Sitzung (Image wie der
Arbeitscontainer, ``/usr/local/bin/mgpt-relay``), nie im Arbeitscontainer. Der
Arbeitscontainer hängt nur an einem internen Podman-Netz ohne Gateway und ohne
DNS; erreichbar ist dort allein dieses Relay. Namen löst das Relay auf, nicht
der Arbeitscontainer. Fällt das Relay aus, ist der Weg zu (fail closed).

Zwei Betriebsarten:

``mgpt-relay --base 20000 host:port [host:port …]``
    Positivliste für ssh/telnet: nimmt auf ``BASE+i`` Verbindungen an und leitet
    sie fest an das i-te freigegebene Ziel weiter. Andere Ziele gibt es nicht.

``mgpt-relay --proxy 3128 host [host …]``
    Netzmodus „Paketquellen“: HTTP-Proxy nur zu den Hosts der vom Server
    signierten Liste. ``CONNECT host:443`` (HTTPS, Tunnel) und ``GET``/``HEAD``
    ``http://host/…`` (Port 80, z. B. ``deb.debian.org``). Hostnamen müssen exakt
    in der Liste stehen; IP-Literale und andere Ports gibt es nicht. Liefert die
    Auflösung eine Adresse in einem privaten, lokalen oder sonst nicht
    öffentlichen Netz, wird die Anfrage abgewiesen (auch gegen DNS-Rebinding:
    verbunden wird genau mit der geprüften Adresse).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import re
import socket
import sys
from urllib.parse import urlsplit

IDLE_SECONDS = 3600

# --- Proxy (Paketquellen) -----------------------------------------------------------------

PROXY_PORT = 3128
#: Hostname mit mindestens einem Punkt und alphabetischer Endung (keine IP-Literale).
HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_HOSTS = 50
MAX_HEAD_BYTES = 16 * 1024
HEAD_TIMEOUT = 30
CONNECT_TIMEOUT = 15
PROXY_IDLE_SECONDS = 600
MAX_CLIENTS = 64
#: Kopfzeilen, die nicht an den Server gehen (hop-by-hop bzw. nur für den Proxy).
HOP_HEADERS = {
    b"connection",
    b"proxy-connection",
    b"keep-alive",
    b"proxy-authorization",
    b"proxy-authenticate",
    b"te",
    b"trailer",
    b"upgrade",
}


class Refused(Exception):
    """Anfrage abgewiesen; ``status`` und kurzer Grund gehen an den Client."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def check_host(host: str) -> str:
    if not HOST_RE.match(host):
        raise ValueError(f"Ungültiger Hostname: {host}")
    return host


def is_public(address: str) -> bool:
    """Nur öffentliche Unicast-Adressen (kein privates, lokales oder Sondernetz)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            ip = mapped
        elif ip.teredo is not None:
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


async def resolve(host: str, port: int, check=is_public) -> list[tuple]:
    """Alle Adressen des Hosts; eine einzige nicht öffentliche verwirft alle (fail closed)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP),
            CONNECT_TIMEOUT,
        )
    except (TimeoutError, OSError) as exc:
        raise Refused(502, "Name nicht auflösbar") from exc
    if not infos:
        raise Refused(502, "Name nicht auflösbar")
    if not all(check(info[4][0]) for info in infos):
        raise Refused(403, "Ziel in einem nicht öffentlichen Netz")
    return infos


async def open_upstream(host: str, port: int, resolver=None, check=is_public):
    """Verbindung zu genau einer geprüften Adresse (keine zweite Auflösung).

    ``resolver``/``check`` nur für Tests austauschbar.
    """
    infos = await (resolver or resolve)(host, port, check)
    last: Exception | None = None
    for family, type_, proto, _canon, sockaddr in infos:
        if not check(sockaddr[0]):
            raise Refused(403, "Ziel in einem nicht öffentlichen Netz")
        sock = socket.socket(family, type_, proto)
        sock.setblocking(False)
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(sock, sockaddr), CONNECT_TIMEOUT
            )
        except (TimeoutError, OSError) as exc:
            sock.close()
            last = exc
            continue
        return await asyncio.open_connection(sock=sock)
    raise Refused(502, "Ziel nicht erreichbar") from last


def _response(status: int, reason: str) -> bytes:
    titles = {
        400: "Bad Request",
        403: "Forbidden",
        405: "Method Not Allowed",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }
    body = f"mgpt-relay: {reason}\n".encode()
    return (
        f"HTTP/1.1 {status} {titles.get(status, 'Error')}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


class Proxy:
    """HTTP-Proxy mit fester Hostliste (CONNECT :443, GET/HEAD http://…:80)."""

    def __init__(
        self, hosts: list[str], resolver=None, max_clients: int = MAX_CLIENTS, check=is_public
    ):
        if not hosts or len(hosts) > MAX_HOSTS:
            raise ValueError("Hostliste leer oder zu lang.")
        self.hosts = frozenset(check_host(h) for h in hosts)
        self.resolver = resolver
        self.check = check
        self.slots = asyncio.Semaphore(max_clients)

    def _allowed(self, host: str | None) -> str:
        host = (host or "").lower()
        if host not in self.hosts:
            raise Refused(403, "Host nicht freigegeben")
        return host

    def parse(self, head: bytes) -> tuple[str, str, int, bytes | None]:
        """Kopf prüfen; liefert (Methode, Host, Port, Anfrage an den Server oder None)."""
        lines = head.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("ascii").split(" ")
        except (UnicodeDecodeError, ValueError) as exc:
            raise Refused(400, "Anfragezeile ungültig") from exc
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            raise Refused(400, "HTTP-Version ungültig")
        headers: list[tuple[bytes, bytes]] = []
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(b":")
            if not sep or not name or name != name.strip() or not re.fullmatch(rb"[!-~]+", name):
                raise Refused(400, "Kopfzeile ungültig")
            headers.append((name, value.strip()))
        if method == "CONNECT":
            host, sep, port = target.rpartition(":")
            if not sep or port != "443":
                raise Refused(403, "Nur Port 443")
            return method, self._allowed(host), 443, None
        if method not in ("GET", "HEAD"):
            raise Refused(405, "Nur CONNECT, GET und HEAD")
        try:
            url = urlsplit(target)
            port = url.port
        except ValueError as exc:
            raise Refused(400, "Adresse ungültig") from exc
        if url.scheme != "http" or url.username is not None or url.password is not None:
            raise Refused(403, "Nur http:// ohne Zugangsdaten")
        if port not in (None, 80):
            raise Refused(403, "Nur Port 80")
        host = self._allowed(url.hostname)
        if any(n.lower() in (b"content-length", b"transfer-encoding") for n, _ in headers):
            raise Refused(400, "Anfrage mit Inhalt")
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        if not path.startswith("/") or any(c in path for c in " \r\n"):
            raise Refused(400, "Pfad ungültig")
        out = [f"{method} {path} HTTP/1.1".encode(), b"Host: " + host.encode()]
        for name, value in headers:
            if name.lower() in HOP_HEADERS or name.lower() == b"host":
                continue
            out.append(name + b": " + value)
        out.append(b"Connection: close")
        return method, host, 80, b"\r\n".join(out) + b"\r\n\r\n"

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.slots.locked():
            await self._refuse(writer, Refused(503, "Zu viele Verbindungen"))
            return
        async with self.slots:
            try:
                try:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
                except asyncio.LimitOverrunError as exc:
                    raise Refused(400, "Kopf zu groß") from exc
                except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                    writer.close()
                    return
                method, host, port, request = self.parse(head[:-4])
                up_reader, up_writer = await open_upstream(host, port, self.resolver, self.check)
            except Refused as refused:
                print(f"mgpt-relay: abgewiesen ({refused.reason})", file=sys.stderr, flush=True)
                await self._refuse(writer, refused)
                return
            if request is None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await asyncio.gather(
                    _pipe(reader, up_writer, PROXY_IDLE_SECONDS),
                    _pipe(up_reader, writer, PROXY_IDLE_SECONDS),
                )
                return
            # GET/HEAD: genau diese eine geprüfte Anfrage, danach nur noch die Antwort
            # (weitere Bytes des Clients gehen nicht an den Server; apt ohne Pipelining).
            up_writer.write(request)
            await _pipe(up_reader, writer, PROXY_IDLE_SECONDS)
            up_writer.close()

    @staticmethod
    async def _refuse(writer: asyncio.StreamWriter, refused: Refused) -> None:
        try:
            writer.write(_response(refused.status, refused.reason))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()


async def serve_proxy(port: int, hosts: list[str], bind: str = "0.0.0.0", resolver=None):
    proxy = Proxy(hosts, resolver)
    server = await asyncio.start_server(proxy.handle, bind, port, limit=MAX_HEAD_BYTES)
    await server.serve_forever()


# --- Positivliste ssh/telnet --------------------------------------------------------------


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, idle: int = IDLE_SECONDS
) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), idle)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--base", type=int)
    mode.add_argument("--proxy", type=int)
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.proxy is not None:
            if args.proxy != PROXY_PORT:
                raise ValueError(f"Proxy nur auf Port {PROXY_PORT}.")
            hosts = [check_host(h) for h in args.targets]
            Proxy(hosts)  # Liste vorab prüfen
            asyncio.run(serve_proxy(args.proxy, hosts))
            return 0
        targets = [parse_target(t) for t in args.targets]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    asyncio.run(serve(args.base if args.base is not None else 20000, targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
