"""Minimaler WebSocket-Client (nur Standardbibliothek) für functional.sh.

Baut über TLS den Handshake zu nginx auf (Host per SNI, Verbindung zu 127.0.0.1),
schickt einen hello-Rahmen und gibt Status, Antwort-Kopfzeilen, den ersten
Textrahmen und den Close-Code als JSON aus.
"""

import base64
import json
import os
import socket
import ssl
import struct
import sys

host, path = sys.argv[1], sys.argv[2]
extra = sys.argv[3:]  # zusätzliche Kopfzeilen "Name: Wert"
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
ctx.set_alpn_protocols(["http/1.1"])
sock = ctx.wrap_socket(
    socket.create_connection(("127.0.0.1", 443), timeout=5), server_hostname=host
)
key = base64.b64encode(os.urandom(16)).decode()
request = [
    f"GET {path} HTTP/1.1",
    f"Host: {host}",
    "Upgrade: websocket",
    "Connection: Upgrade",
    f"Sec-WebSocket-Key: {key}",
    "Sec-WebSocket-Version: 13",
    *extra,
]
sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode())
f = sock.makefile("rb")
status = int(f.readline().split()[1])
headers = {}
while line := f.readline().strip():
    name, _, value = line.decode().partition(":")
    headers[name.strip().lower()] = value.strip()
out = {"status": status, "headers": headers, "text": None, "close": None}
if status == 101:
    # Gültiger hello-Rahmen (Protokoll v1) mit einem Token, den es nicht gibt.
    hello = {
        "type": "hello",
        "token": "mgrt_" + "A" * 43,
        "protocol": 1,
        "agent_version": "0",
        "engine": {"name": "fake", "version": "0", "rootless": True},
        "hostname": "paketpruefung",
    }
    payload = json.dumps(hello).encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = (
        bytes([0x80 | len(payload)])
        if len(payload) < 126
        else (bytes([0x80 | 126]) + struct.pack("!H", len(payload)))
    )
    sock.sendall(bytes([0x81]) + length + mask + masked)
    while out["close"] is None:
        head = f.read(2)
        if len(head) < 2:
            break
        n = head[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", f.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", f.read(8))[0]
        data = f.read(n)
        if head[0] & 0x0F == 0x1:
            out["text"] = json.loads(data)
        elif head[0] & 0x0F == 0x8:
            out["close"] = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
print(json.dumps(out))
