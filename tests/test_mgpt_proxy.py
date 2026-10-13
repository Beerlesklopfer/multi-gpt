"""Proxy-Modus von ``mgpt-relay`` (Netzmodus „Paketquellen“, M14).

Ohne Datenbank und ohne Container: Der Proxy läuft im Test an 127.0.0.1, die
Auflösung ersetzt ein fester Resolver. Geprüft wird, dass nur freigegebene Hosts
über CONNECT :443 bzw. GET http://…:80 erreichbar sind und dass nicht öffentliche
Adressen (auch nach der Auflösung) abgewiesen werden.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from runner_agent import mgpt_relay

HOSTS = ["deb.debian.org", "pypi.org"]


def _loopback_ok(address: str) -> bool:
    """Im Test gilt 127.0.0.1 als „öffentlich“, sonst die echte Prüfung."""
    return address == "127.0.0.1" or mgpt_relay.is_public(address)


def _resolver(table: dict[str, list[str]]):
    async def resolve(host, port, check):
        addresses = table.get(host)
        if not addresses:
            raise mgpt_relay.Refused(502, "Name nicht auflösbar")
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (a, port))
            for a in addresses
        ]
        if not all(check(i[4][0]) for i in infos):
            raise mgpt_relay.Refused(403, "Ziel in einem nicht öffentlichen Netz")
        return infos

    return resolve


async def _upstream():
    """Ziel-Server: antwortet auf HTTP mit dem empfangenen Kopf, sonst Echo."""
    seen: list[bytes] = []

    async def handle(reader, writer):
        data = await reader.read(65536)
        seen.append(data)
        if data.startswith((b"GET ", b"HEAD ")):
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n" + body
            )
        else:
            writer.write(b"echo:" + data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


async def _proxy(table, upstream_port):
    """Proxy, dessen Verbindungen auf 443/80 zum Test-Ziel umgelenkt werden."""

    async def resolve(host, port, check):
        infos = await _resolver(table)(host, port, check)
        return [(f, t, p, c, (a[0], upstream_port)) for f, t, p, c, a in infos]

    proxy = mgpt_relay.Proxy(HOSTS, resolver=resolve, check=_loopback_ok)
    server = await asyncio.start_server(
        proxy.handle, "127.0.0.1", 0, limit=mgpt_relay.MAX_HEAD_BYTES
    )
    return server, server.sockets[0].getsockname()[1]


async def _ask(port: int, request: bytes, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    if then:
        line = await reader.readuntil(b"\r\n\r\n")
        writer.write(then)
        await writer.drain()
        rest = await asyncio.wait_for(reader.read(65536), 5)
        writer.close()
        return line + rest
    data = await asyncio.wait_for(reader.read(65536), 5)
    writer.close()
    return data


def _run(request: bytes, then: bytes = b"", table=None):
    table = table or {h: ["127.0.0.1"] for h in HOSTS}

    async def main():
        upstream, up_port, seen = await _upstream()
        proxy, port = await _proxy(table, up_port)
        async with upstream, proxy:
            answer = await _ask(port, request, then)
        return answer, seen

    return asyncio.run(main())


# --- Erlaubte Wege ------------------------------------------------------------------------


def test_connect_to_listed_host_tunnels():
    answer, seen = _run(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n", b"hallo")
    assert answer.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert answer.endswith(b"echo:hallo")
    assert seen == [b"hallo"]


def test_get_to_listed_host_is_rewritten_to_origin_form():
    answer, seen = _run(
        b"GET http://deb.debian.org/debian/dists/trixie/InRelease HTTP/1.1\r\n"
        b"Host: deb.debian.org\r\nProxy-Connection: keep-alive\r\n"
        b"Proxy-Authorization: Basic eA==\r\nUser-Agent: apt\r\n\r\n"
    )
    assert answer.startswith(b"HTTP/1.1 200 OK")
    request = seen[0]
    assert request.startswith(b"GET /debian/dists/trixie/InRelease HTTP/1.1\r\n")
    assert b"Host: deb.debian.org\r\n" in request
    assert b"User-Agent: apt\r\n" in request
    assert b"Proxy-" not in request
    assert request.endswith(b"Connection: close\r\n\r\n")


def test_host_match_ignores_case_only():
    answer, _ = _run(b"CONNECT PyPI.org:443 HTTP/1.1\r\n\r\n", b"x")
    assert answer.startswith(b"HTTP/1.1 200")


# --- Abweisungen --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_bytes", "status"),
    [
        (b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT pypi.org.evil.example:443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT sub.pypi.org:443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT pypi.org.:443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT pypi.org:22 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT pypi.org:8443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT pypi.org HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT 151.101.0.223:443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", 403),
        (b"GET http://evil.example/ HTTP/1.1\r\n\r\n", 403),
        (b"GET http://deb.debian.org:8080/ HTTP/1.1\r\n\r\n", 403),
        (b"GET http://deb.debian.org:443/ HTTP/1.1\r\n\r\n", 403),
        (b"GET https://deb.debian.org/ HTTP/1.1\r\n\r\n", 403),
        (b"GET http://user@deb.debian.org/ HTTP/1.1\r\n\r\n", 403),
        (b"GET http://deb.debian.org\\@evil.example/ HTTP/1.1\r\n\r\n", 403),
        (b"GET http://192.168.1.1/ HTTP/1.1\r\n\r\n", 403),
        (b"GET /debian/ HTTP/1.1\r\nHost: deb.debian.org\r\n\r\n", 403),
        (b"POST http://deb.debian.org/ HTTP/1.1\r\nContent-Length: 0\r\n\r\n", 405),
        (b"PUT http://deb.debian.org/ HTTP/1.1\r\n\r\n", 405),
        (b"GET http://deb.debian.org/ HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc", 400),
        (b"GET http://deb.debian.org/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", 400),
        (b"GET http://deb.debian.org/ HTTP/2.0\r\n\r\n", 400),
        (b"GET  http://deb.debian.org/ HTTP/1.1\r\n\r\n", 400),
        (b"GET http://deb.debian.org/ HTTP/1.1\r\nBad Header: x\r\n\r\n", 400),
    ],
)
def test_refused_requests_never_reach_upstream(request_bytes, status):
    answer, seen = _run(request_bytes)
    assert answer.startswith(f"HTTP/1.1 {status} ".encode()), answer
    assert b"Connection: close" in answer
    assert seen == []


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "192.168.178.1", "127.0.0.2", "169.254.169.254", "100.64.0.1", "0.0.0.0"],
)
def test_private_address_after_resolution_is_refused(address):
    """DNS-Rebinding: der freigegebene Name zeigt auf ein lokales Netz."""
    table = {"pypi.org": [address], "deb.debian.org": ["127.0.0.1"]}
    answer, seen = _run(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", table=table)
    assert answer.startswith(b"HTTP/1.1 403 ")
    assert seen == []


def test_one_private_address_refuses_all():
    table = {"pypi.org": ["127.0.0.1", "10.1.2.3"], "deb.debian.org": ["127.0.0.1"]}
    answer, seen = _run(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", table=table)
    assert answer.startswith(b"HTTP/1.1 403 ")
    assert seen == []


def test_unresolvable_name_fails_closed():
    answer, seen = _run(
        b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", table={"deb.debian.org": ["1.1.1.1"]}
    )
    assert answer.startswith(b"HTTP/1.1 502 ")
    assert seen == []


def test_oversized_head_is_refused():
    answer, seen = _run(
        b"GET http://deb.debian.org/ HTTP/1.1\r\nX: "
        + b"a" * (mgpt_relay.MAX_HEAD_BYTES + 10)
        + b"\r\n\r\n"
    )
    assert answer.startswith(b"HTTP/1.1 400 ")
    assert seen == []


def test_too_many_clients_get_503():
    async def main():
        proxy = mgpt_relay.Proxy(HOSTS, resolver=_resolver({}), max_clients=1, check=_loopback_ok)
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            first = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)
            answer = await _ask(port, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
            first[1].close()
        return answer

    assert asyncio.run(main()).startswith(b"HTTP/1.1 503 ")


# --- Adressen und Aufruf ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("151.101.2.132", True),
        ("2a04:4e42::644", True),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.0.1", False),
        ("127.0.0.1", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fd00::1", False),
        ("::ffff:192.168.0.1", False),
        ("::ffff:151.101.2.132", True),
        ("2002:c0a8:0001::1", False),
        ("64:ff9b::a00:1", False),
        ("kein-ip", False),
    ],
)
def test_is_public(address, public):
    assert mgpt_relay.is_public(address) is public


def test_real_resolver_refuses_localhost_names():
    """Echte Auflösung: ein Name auf 127.0.0.1 wird nie verbunden."""
    with pytest.raises(mgpt_relay.Refused) as info:
        asyncio.run(mgpt_relay.resolve("localhost", 443))
    assert info.value.status in (403, 502)


@pytest.mark.parametrize(
    "host",
    ["deb.debian.org", "files.pythonhosted.org", "index.crates.io", "a.bc"],
)
def test_host_list_accepts_names(host):
    assert mgpt_relay.check_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "1.2.3.4",
        "deb.debian.org:80",
        "Deb.debian.org",
        "-a.org",
        "a..org",
        "*.debian.org",
        "deb.debian.org.",
        "",
        "a" * 64 + ".org",
        "[::1]",
    ],
)
def test_host_list_rejects_others(host):
    with pytest.raises(ValueError):
        mgpt_relay.check_host(host)


def test_main_rejects_bad_proxy_arguments(capsys):
    assert mgpt_relay.main(["--proxy", "8080", "pypi.org"]) == 2
    assert mgpt_relay.main(["--proxy", "3128", "10.0.0.1"]) == 2
    assert mgpt_relay.main(["--proxy", "3128", *[f"h{i}.org" for i in range(51)]]) == 2
    assert "Proxy nur auf Port 3128" in capsys.readouterr().err


def test_get_forwards_only_the_checked_request():
    """Nach dem Kopf geschickte Bytes (z. B. eine zweite Anfrage) gehen nicht weiter."""
    answer, seen = _run(
        b"GET http://deb.debian.org/a HTTP/1.1\r\n\r\nGET http://evil.example/b HTTP/1.1\r\n\r\n"
    )
    assert answer.startswith(b"HTTP/1.1 200 OK")
    assert len(seen) == 1
    assert b"evil" not in seen[0]
