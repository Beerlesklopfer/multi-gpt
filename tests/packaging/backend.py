import base64
import hashlib
import json
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def websocket(self):
        """Runner-Dienst (M14) im Kleinen: 101, Kopfzeilen als Textrahmen, Close 4401."""
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        # Erster Rahmen des Clients (hello, maskiert) lesen und verwerfen.
        head = self.rfile.read(2)
        n = head[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.rfile.read(8))[0]
        self.rfile.read(4 + n)
        info = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
        self.wfile.write(bytes([0x81, 126]) + struct.pack("!H", len(info)) + info)
        reason = struct.pack("!H", 4401) + b"token"
        self.wfile.write(bytes([0x88, len(reason)]) + reason)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.websocket()
            return
        if self.path.startswith("/api/conversations/1/messages/") or self.path == "/mcp/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                data = f"data: {i} {time.time():.3f}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                time.sleep(1)
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path == "/download":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", "attachment; filename=x.txt")
            self.send_header("X-Accel-Redirect", "/_protected/media/documents/1/abc.txt")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        if self.path.rstrip("/") == "/mcp":
            # MCP-Server (M15): Antwort als SSE mit Fortschritt
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(3):
                data = f"data: {i} {time.time():.3f}\n\n".encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                time.sleep(1)
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path.startswith("/mcp/messages"):
            # Legacy-SSE (M15): 202 mit Pfad samt Query (Sitzung kommt an?)
            body = json.dumps({"path": self.path, "got": n}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = f"got {n}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
